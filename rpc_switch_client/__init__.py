
# stdlib
import asyncio
import base64
import hashlib
import json
import logging
import os
import pprint
import ssl as ssl2  # ugh
import sys
import traceback

# pypi
import pynetstring

# based on https://www.jsonrpc.org/specification
NOT_NOTIFICATION  = -32000
METHOD_ERROR      = -32001
INVALID_REQUEST   = -32600
METHOD_NOT_FOUND  = -32601
INVALID_PARAMS    = -32602
INTERNAL_ERROR    = -32603
PARSE_ERROR       = -32700

RES_OK                 = 'RES_OK'
RES_WAIT               = 'RES_WAIT'
RES_TIMEOUT            = 'RES_TIMEOUT'
RES_ERROR              = 'RES_ERROR'
RES_OTHER              = 'RES_OTHER' # 'dunno'
WORK_OK                = 0           # exit codes for work() method
WORK_PING_TIMEOUT      = 92
WORK_CONNECTION_CLOSED = 91

pp = pprint.PrettyPrinter(indent=4)


class Error(Exception):
    """Base class for client errors."""


class JSON_RPC_Error(Error):
    """JSON-RPC 2.0 protocol errors."""

    def __init__(self, code, message, data=None):
        super().__init__(self)
        self.code = code
        self.message = message
        self.data = data

    # do we need repr?
    #def __repr__(self):
    #    if self.data:
    #        return "<%s %s: %s %r>" % (self.__class__.__name__, self.code,
    #                                   self.message, self.data)
    #    else:
    #        return "<%s %s: %s>" % (self.__class__.__name__, self.code,
    #                                self.message)

    def __str__(self):
        if self.data:
            return "<<%s %s: %s %r>>" % (
                self.__class__.__name__,
                self.code,
                self.message,
                self.data,
            )
        else:
            return "<<%s %s: %s>>" % (
                self.__class__.__name__,
                self.code,
                self.message,
            )


class RPC_Switch_Client_Error(JSON_RPC_Error):
    """errors raised by callbacks."""

    def __init__(self, message):
        super().__init__(METHOD_ERROR, message)


class RPC_Switch_Client:
    """todo: doc"""

    #
    # internal stuff
    #

    def __init__(
        self,
        *,
        who,
        token,
        method="password",
        host="localhost",
        port=6551,
        local_addr=None,
        tls=False,
        tls_cert=None,
        tls_key=None,
        tls_ca=None,
        tls_server_hostname=None,
        json=False,
        logger=None,
        level=None,
    ):
        self.who = who
        self.token = token
        self.method = method
        self.host = host
        self.port = port
        self.local_addr=local_addr
        self.tls=tls
        self.tls_server_hostname=tls_server_hostname
        self.json = json
        self.workername = None
        self._decoder = pynetstring.Decoder()
        if logger:
            self._logger = logger
        else:
            self._logger = logging.getLogger(__name__)
        if level:
            self._logger.setLevel(level)
        self.__calls = {}  # ids of call we're waiting for
        self.__id = 0
        self.__loop = asyncio.get_running_loop()
        self.__methods = {
            "rpcswitch.channel_gone": {"cb": self._rpc_channel_gone, "nf": True},
            "rpcswitch.greetings": {"cb": self._rpc_greetings, "nf": True},
            "rpcswitch.ping": {"cb": self._rpc_ping},
            "rpcswitch.result": {"cb": self._rpc_results, "nf": True},
        }
        self.__tasks = {}
        self.__waits = {}  # waitids for result notifications
        self.tls=False
        if tls:
            self._debug("setting up tls")
            ssl_context = ssl2.create_default_context(
                purpose=ssl2.Purpose.SERVER_AUTH,
                cafile=tls_ca,
            )
            if tls_key:
                ssl_context.verify_mode = ssl2.CERT_REQUIRED
                ssl_context.load_cert_chain(
                    certfile=tls_cert,
                    keyfile=tls_key,
                )
            self.tls = ssl_context            

    def _debug(self, d):
        self._logger.debug(d)

    #
    # rpc callbacks
    #

    def _rpc_channel_gone(self, args):
        pass  # ignore for now..

    def _rpc_greetings(self, args):
        self._debug(f"greetings: {pp.pformat(args)}")
        try:
            self.__greetings.set_result(args)
        except AttributeError:
            raise Error("greetings while already connected?")

    def _rpc_ping(self, ping):
        return "pong"

    def _rpc_results(self, args):
        status, waitid, result = args
        c = self.__waits.pop(waitid, None)
        #self._debug(f'_rpc_results c: {pp.pformat(c)}')
        if not c or c.cancelled():
            # error?
            return
        # check for future?
        if status == RES_OK:
            c.set_result(result)
        elif status == RES_ERROR:
            c.set_exception(RPC_Switch_Client_Error(result))
        else:
            raise RPC_Switch_Client_Error(f'unknown status {status}')

    #
    # network data handling
    #
        
    async def _handle(self):
        self._debug('in _handle')
        while True:
            data = await self.reader.read(10000)  # FIXME: read size?
            if not data:
                # eof?
                self._debug("_handle bailing out!")
                return WORK_CONNECTION_CLOSED
            # self._debug(f"_handle got data: {data!r}")
            decoded_list = self._decoder.feed(data)  # FIXME: try?
            for item in decoded_list:
                reqid = None
                try:
                    try:
                        jrpc = json.loads(item.decode())
                    except json.JSONDecodeError:
                        raise JSON_RPC_Error(PARSE_ERROR, "invalid JSON")
                    self._debug(f"R {jrpc!r}")
                    if not isinstance(jrpc, dict):
                        raise JSON_RPC_Error(INVALID_REQUEST, "not a JSON object")
                    if jrpc.get("jsonrpc", "") != "2.0":
                        raise JSON_RPC_Error(INVALID_REQUEST, "not JSON-RPC 2.0")
                    if "id" in jrpc:
                        reqid = jrpc["id"]
                        if reqid and not (
                            isinstance(reqid, str) or isinstance(reqid, int)
                        ):
                            raise JSON_RPC_Error(
                                INVALID_REQUEST, "id is not a string or number"
                            )
                    if "method" in jrpc:
                        try:
                            await self._handle_request(jrpc)
                        except JSON_RPC_Error:
                            raise
                        except Exception:
                            exc_type, exc_value, exc_traceback = sys.exc_info()
                            raise RPC_Switch_Client_Error(traceback.format_exception(exc_type, exc_value, exc_traceback))
                    elif reqid and ("result" in jrpc or "error" in jrpc):
                        self._handle_response(jrpc)
                    else:
                        raise JSON_RPC_Error(
                            INVALID_REQUEST, "invalid JSON_RPC object!"
                        )
                except JSON_RPC_Error as err:
                    res = {
                        "jsonrpc": "2.0",
                        "id": reqid,
                        "error": {
                            "code": err.code,
                            "message": err.message
                        }
                    }
                    if err.data:
                        res["error"]["data"] = data
                    self._debug(f"E {res!r}")
                    self.writer.write(pynetstring.encode(json.dumps(res).encode()))

    async def _handle_request(self, jrpc):
        method = jrpc["method"]
        if not method in self.__methods:
            raise JSON_RPC_Error(METHOD_NOT_FOUND, f"no such method '{method}'")
        m = self.__methods[method]
        #self._debug("\n".join("{}\t{}".format(k, v) for k, v in m.items()))
        nf = m.get("nf")
        if not nf and not "id" in jrpc:
            raise JSON_RPC_Error(
                NOT_NOTIFICATION, f"method '{method}' is not a notification"
            )
        reqid = jrpc.get("id")
        if not "params" in jrpc:
            raise JSON_RPC_Error(
                INVALID_REQUEST, f"no parameters given for method '{method}'"
            )
        params = jrpc["params"]
        if not (isinstance(params, dict) or isinstance(params, list)):
            raise JSON_RPC_Error(INVALID_REQUEST, f"params should be array or object")
        ret = {
            "jsonrpc": "2.0",
            "id": reqid
        }
        if m.get("magic"):
            # announced methods
            rpcswitch = jrpc["rpcswitch"]
            assert rpcswitch["vcookie"] == "eatme"
            ret["rpcswitch"] = rpcswitch
            try:
                mode = m["mode"]
                if mode == "async":
                    res = [
                        RES_WAIT,
                        await self._async_wrapper(rpcswitch, m["cb"], reqid, params),
                    ]
                elif mode == "sync":
                    res = [RES_OK, m["cb"](reqid, params)]
                elif mode == "subproc":
                    res = [RES_ERROR, "mode subproc not implemented"]
                else:
                    res = [RES_ERROR, f"invalid mode '{mode}'"]
            except Exception:
                exc_type, exc_value, exc_traceback = sys.exc_info()
                res = [RES_ERROR, traceback.format_exception(exc_type, exc_value, exc_traceback)]
        else:
            # internal rpcswitch.* methods
            res = m["cb"](params)
        ret["result"] = res
        if nf:
            self._debug(f"N {ret!r}")
            return
        self._debug(f"W {ret!r}")
        self.writer.write(pynetstring.encode(json.dumps(ret).encode()))

    async def _async_wrapper(self, rpcswitch, cb, reqid, params):
        t = asyncio.create_task(cb(reqid, params))
        waitid = (
            base64.b64encode(hashlib.md5((str(id(t)) + str(reqid)).encode("utf-8")).digest())
            .decode("utf-8")
            .strip("=")
        )

        def done(t):
            self._debug(f"in done handler for waitid {waitid} and task {t!s}")
            status = RES_OK
            try:
                result = t.result()
            except Exception:
                status = RES_ERROR
                exc_type, exc_value, exc_traceback = sys.exc_info()
                result = traceback.format_exception(exc_type, exc_value, exc_traceback)
            req = {
                "jsonrpc": "2.0",
                "method": "rpcswitch.result",
                "rpcswitch": rpcswitch,
                "params": [status, waitid, result],
            }
            self._debug(f"W {req!r}")
            self.writer.write(pynetstring.encode(json.dumps(req).encode()))

        t.add_done_callback(done)
        self._debug(f"returing waitid {waitid} for task {t!s}")
        return waitid

    def _handle_response(self, jrpc):
        reqid = jrpc["id"]
        c = self.__calls.pop(reqid, None)
        #self._debug(f'_handle_response c: {pp.pformat(c)}')
        if not c or c.cancelled():
            # error?
            return
        # check for future?
        if "result" in jrpc:
            #self._debug(f'set_result')
            status, result = jrpc["result"]  # fixme: check for list
            if status == RES_WAIT:
                self._debug(f'got RES_WAIT {result} for id {reqid}')
                self.__waits[result] = c
                return
            else:
                c.set_result(jrpc["result"])
        elif "error" in jrpc:
            #self._debug(f'set_exception')
            c.set_exception(RPC_Switch_Client_Error(jrpc["error"]))
        # else?

    #
    # public stuff
    #

    async def connect(self):
        if self.tls: 
            self._debug(f"doing ssl {self.tls_server_hostname}")
            reader, writer = await asyncio.open_connection(
                host=self.host,
                port=self.port,
                local_addr=self.local_addr,
                ssl=self.tls,
                server_hostname=self.tls_server_hostname,
            )
        else:
            reader, writer = await asyncio.open_connection(
                host=self.host,
                port=self.port,
                local_addr=self.local_addr,
            )

        self.reader = reader
        self.writer = writer

        self.__greetings = self.__loop.create_future()
        self.__handle_task = asyncio.create_task(self._handle())

        try:
            greetings = await asyncio.wait_for(self.__greetings, timeout=10)
            del self.__greetings
        except asyncio.TimeoutError:
            raise Exception("uh? timeout or so?'")

        try:
            if greetings["who"] == "rpcswitch" and greetings["version"] == "1.0":
                self._logger.info(f"connected: {pp.pformat(greetings)}")
            else:
                raise JSON_RPC_Error(
                    f"received invalid greetings: {pp.pformat(greetings)}"
                )
        except KeyError:            
            raise JSON_RPC_Error(f"received weird greetings: {pp.pformat(greetings)}")
                
        hello = self.call(
            "rpcswitch.hello",
            {"who":self.who, "token":self.token, "method":self.method}
        )
        self._debug("waiting for hello")
        hello = await hello
        self._debug(f"hello: {pp.pformat(hello)}")
        if not hello[0]:
            raise RPC_Switch_Client_Error(hello[1])
        self._logger.info(f"authenticated: {hello[1]}")
        return hello[1]

    async def announce(self, *, method, cb, mode="sync", workername=None):
        if not self.workername:
            if workername:
                self.workername = workername
            else:
                self.workername = (
                    f"{self.who} {os.uname()[1]} {sys.argv[0]} {os.getpid()}"
                )
        if mode not in ["sync", "async", "subproc"]:
            raise RPC_Switch_Client_Error(f"invalid mode {mode}")
        if method in self.__methods:
            raise RPC_Switch_Client_Error(f"already announced method {method}?")
        res = await self.call("rpcswitch.announce", {
            "workername": self.workername,
            "method": method,
        })
        if not res[0]:
            raise RPC_Switch_Client_Error(res[1])
        self.__methods[method] = {
            'cb': cb,
            'magic': True,
            'mode': mode
        }
        self._debug(f"announce: {pp.pformat(res)}")
        return res[1]

    async def work(self):
        try:
            ret = await self.__handle_task
        except asyncio.CancelledError:
            ret = 0
        return ret

    async def stop(self):
        self.__handle_task.cancel()

    async def close(self):
        self._debug("closing down..")
        self.writer.close()
        await self.writer.wait_closed()

    async def call(self, method, params):
        f = self.__loop.create_future()
        self.__id += 1
        reqid = (base64.b64encode(
                hashlib.md5(
                    f"{self.__id}{method}{params!s}{id(f)}".encode("utf-8")
                ).digest()
            ).decode("utf-8").strip("="))
        req = {
               "jsonrpc": "2.0",
               "method": method,
               "params": params,
               "id": reqid
            }
        self._debug(f"W {req!r}")
        self.__calls[reqid] = f
        self.writer.write(pynetstring.encode(json.dumps(req).encode()))
        return await f
