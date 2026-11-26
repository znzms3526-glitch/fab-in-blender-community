"""Per-destination proxy selection and bounded, read-only network measurements."""
import ctypes
import errno
import http.client
import ipaddress
import re
import socket
import statistics
import sys
import time
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from ctypes import wintypes
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from functools import lru_cache, partial
from itertools import count
from threading import Lock
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import (HTTPHandler, HTTPRedirectHandler, HTTPSHandler, ProxyHandler,
                            Request, build_opener, getproxies, proxy_bypass)

DEFAULTS=dict(language="zh-CN",apiRoute="system",fileRoute="system",proxy="",range="quixel",tier="personal",
              background=True,coverMiB=512)
LABELS={"system":"跟随系统","direct":"直连","proxy":"指定代理"}
FILE_LABELS={**LABELS,"auto":"自动选路"}
API_PROBE="https://www.fab.com/i/taxonomy/listing-types"
PAC_TTL=10


def proxy_address(value):
    value=value.strip()
    if not value:return ""
    try:
        p=urlsplit(value if "://" in value else "http://"+value)
        if (p.scheme not in ("http","socks5","socks5h") or not p.hostname or not p.port or
            p.username is not None or p.path not in ("","/") or "?" in value or "#" in value or
            any(c.isspace() for c in value)):
            raise ValueError
        host=p.hostname
        try:
            ip=ipaddress.ip_address(host)
            host=f"[{ip}]" if ip.version==6 else str(ip)
        except ValueError:
            if not re.fullmatch(r"[a-z0-9.-]+",host,re.I):raise ValueError
        return f"{'http' if p.scheme=='http' else 'socks5'}://{host}:{p.port}"
    except ValueError:
        raise ValueError("请输入 HTTP 或 SOCKS5 代理的主机和端口，例如 127.0.0.1:7890") from None


def validate(values):
    s={k:values.get(k,v) for k,v in DEFAULTS.items()}
    if s["language"] not in ("en","zh-CN"):raise ValueError("界面语言无效")
    if s["apiRoute"] not in LABELS or s["fileRoute"] not in FILE_LABELS:raise ValueError("连接方式无效")
    s["proxy"]=proxy_address(s["proxy"])
    if "proxy" in (s["apiRoute"],s["fileRoute"]) and not s["proxy"]:raise ValueError("请填写代理地址")
    if s["range"] not in {"all","quixel"} or s["tier"] not in {"professional","personal"}:raise ValueError("默认许可或范围无效")
    if not isinstance(s["background"],bool) or s["coverMiB"] not in (128,256,512,1024,2048):raise ValueError("设置值无效")
    return s


class RouteError(OSError):
    pass


@dataclass(frozen=True)
class Route:
    proxy: str
    label: str

    @property
    def signature(self):
        return self.proxy


def _proxy_label(proxy):
    p=urlsplit(proxy)
    return ("SOCKS5" if p.scheme=="socks5" else "HTTP")+" "+p.netloc


def _system_route(proxy="", source=""):
    return Route(proxy,"跟随系统 · "+(source+" · " if source else "")+
                 (_proxy_label(proxy) if proxy else "直连或 TUN"))


class _IEConfig(ctypes.Structure):
    _fields_=[("auto",wintypes.BOOL),("pac",ctypes.c_void_p),("proxy",ctypes.c_void_p),("bypass",ctypes.c_void_p)]


class _AutoProxyOptions(ctypes.Structure):
    _fields_=[("flags",wintypes.DWORD),("discovery",wintypes.DWORD),("pac",wintypes.LPCWSTR),
              ("reserved_ptr",ctypes.c_void_p),("reserved",wintypes.DWORD),("auto_logon",wintypes.BOOL)]


class _ProxyEntry(ctypes.Structure):
    _fields_=[("is_proxy",wintypes.BOOL),("bypass",wintypes.BOOL),("scheme",ctypes.c_int),
              ("host",wintypes.LPWSTR),("port",wintypes.WORD)]


class _ProxyResult(ctypes.Structure):
    _fields_=[("count",wintypes.DWORD),("entries",ctypes.POINTER(_ProxyEntry))]


class _AsyncResult(ctypes.Structure):
    _fields_=[("result",ctypes.c_size_t),("error",wintypes.DWORD)]


WINHTTP_NO_PROXY=1
WINHTTP_ASYNC=0x10000000
WINHTTP_REQUEST_ERROR=0x00200000
WINHTTP_PROXY_COMPLETE=0x01000000
ERROR_IO_PENDING=997
WINHTTP_TIMEOUT=12002
AUTO_DETECT=1
CONFIG_URL=2
DETECT_DHCP_DNS=3
NO_CACHE=0x00080000 | 0x00100000
AUTODETECTION_FAILED=12180
_native_sequence=count(1)
_native_pending={}
_native_lock=Lock()


def _winhttp_complete(handle, context, status, info, length):
    with _native_lock:
        completion=_native_pending.get(context)
        if completion is None or completion.done():return
        if status==WINHTTP_REQUEST_ERROR and info and length>=ctypes.sizeof(_AsyncResult):
            completion.set_result(ctypes.cast(info,ctypes.POINTER(_AsyncResult)).contents.error)
        elif status==WINHTTP_PROXY_COMPLETE:
            completion.set_result(0)


@lru_cache(maxsize=1)
def _winhttp():
    dll=ctypes.WinDLL("winhttp",use_last_error=True)
    kernel=ctypes.WinDLL("kernel32",use_last_error=True)
    pointer=ctypes.POINTER
    handle=ctypes.c_void_p
    callback_type=ctypes.WINFUNCTYPE(None,handle,ctypes.c_size_t,wintypes.DWORD,handle,wintypes.DWORD)
    callback=callback_type(_winhttp_complete)
    for library,name,result,args in (
        (dll,"WinHttpGetIEProxyConfigForCurrentUser",wintypes.BOOL,[pointer(_IEConfig)]),
        (dll,"WinHttpOpen",handle,[wintypes.LPCWSTR,wintypes.DWORD,wintypes.LPCWSTR,wintypes.LPCWSTR,wintypes.DWORD]),
        (dll,"WinHttpSetTimeouts",wintypes.BOOL,[handle,ctypes.c_int,ctypes.c_int,ctypes.c_int,ctypes.c_int]),
        (dll,"WinHttpSetStatusCallback",handle,[handle,callback_type,wintypes.DWORD,ctypes.c_size_t]),
        (dll,"WinHttpCreateProxyResolver",wintypes.DWORD,[handle,pointer(handle)]),
        (dll,"WinHttpGetProxyForUrlEx",wintypes.DWORD,[handle,wintypes.LPCWSTR,pointer(_AutoProxyOptions),ctypes.c_size_t]),
        (dll,"WinHttpGetProxyResult",wintypes.DWORD,[handle,pointer(_ProxyResult)]),
        (dll,"WinHttpFreeProxyResult",None,[pointer(_ProxyResult)]),
        (dll,"WinHttpCloseHandle",wintypes.BOOL,[handle]),
        (kernel,"GlobalFree",handle,[handle]),
    ):
        fn=getattr(library,name)
        fn.restype,fn.argtypes=result,args
    # Keep the callback alive for native workers, including cancellation after a timeout.
    return dll,kernel.GlobalFree,callback


def _strings(record, free, *fields):
    pointers=[getattr(record,field) for field in fields]
    try:return tuple(ctypes.wstring_at(p) if p else "" for p in pointers)
    finally:
        for p in pointers:
            if p:free(p)


def _windows_config():
    dll,free,_=_winhttp()
    config=_IEConfig()
    ok=dll.WinHttpGetIEProxyConfigForCurrentUser(ctypes.byref(config))
    error=ctypes.get_last_error()
    values=_strings(config,free,"pac","proxy","bypass")
    if not ok:raise ctypes.WinError(error)
    return bool(config.auto),*values


def _windows_pac(target, auto, pac):
    dll,_,callback=_winhttp()
    handle=dll.WinHttpOpen("FabShelf",WINHTTP_NO_PROXY,None,None,WINHTTP_ASYNC)
    if not handle:raise ctypes.WinError(ctypes.get_last_error())
    resolver=ctypes.c_void_p()
    with _native_lock:
        context=next(_native_sequence)
        completion=_native_pending[context]=Future()
    try:
        dll.WinHttpSetTimeouts(handle,2500,2500,2500,3000)
        previous=dll.WinHttpSetStatusCallback(handle,callback,WINHTTP_REQUEST_ERROR | WINHTTP_PROXY_COMPLETE,0)
        if previous==ctypes.c_void_p(-1).value:raise ctypes.WinError(ctypes.get_last_error())
        error=dll.WinHttpCreateProxyResolver(handle,ctypes.byref(resolver))
        if error:raise ctypes.WinError(error)
        # Use the configured PAC first. Do not send integrated credentials to fetch a script.
        options=_AutoProxyOptions(NO_CACHE | (CONFIG_URL if pac else AUTO_DETECT),
                                  DETECT_DHCP_DNS if auto and not pac else 0,pac or None,None,0,False)
        error=dll.WinHttpGetProxyForUrlEx(resolver,target,ctypes.byref(options),context)
        if error==ERROR_IO_PENDING:
            try:error=completion.result(timeout=5)
            except TimeoutError:raise ctypes.WinError(WINHTTP_TIMEOUT) from None
        if error:raise ctypes.WinError(error)
        result=_ProxyResult()
        error=dll.WinHttpGetProxyResult(resolver,ctypes.byref(result))
        if error:raise ctypes.WinError(error)
        try:
            if not result.count or not result.entries:raise RouteError("系统 PAC 返回无效线路")
            # The synchronous API strips SOCKS entries. The async result preserves their order.
            first=result.entries[0]
            if not first.is_proxy:return _system_route(source="PAC 绕过" if first.bypass else "PAC")
            scheme={1:"http",2:"https",4:"socks5"}.get(first.scheme)
            if not scheme or not first.host or not first.port:raise RouteError("系统 PAC 返回无效线路")
            host=first.host
            if ":" in host and not host.startswith("["):host=f"[{host}]"
            return _system_route(_configured_address(f"{scheme}://{host}:{first.port}"),source="PAC")
        finally:dll.WinHttpFreeProxyResult(ctypes.byref(result))
    finally:
        with _native_lock:_native_pending.pop(context,None)
        if resolver:dll.WinHttpCloseHandle(resolver)
        dll.WinHttpCloseHandle(handle)


_pac_cache=OrderedDict()
_pac_pending={}
_pac_lock=Lock()


def _pac_lookup(target, config):
    key=target,config
    with _pac_lock:
        cached=_pac_cache.get(key)
        if cached and time.monotonic()<cached[0]:
            _pac_cache.move_to_end(key)
            return cached[1]
        future=_pac_pending.get(key)
        owner=future is None
        if owner:
            future=_pac_pending[key]=Future()
    if not owner:return future.result()
    try:
        try:result=_windows_pac(target,*config[:2])
        except OSError as error:
            # Cache only a numeric error, without an exception traceback or PAC URL.
            result=getattr(error,"winerror",None) or 0
        with _pac_lock:
            _pac_cache[key]=(time.monotonic()+PAC_TTL,result)
            _pac_cache.move_to_end(key)
            if len(_pac_cache)>128:_pac_cache.popitem(last=False)
        future.set_result(result)
        return result
    except BaseException as error:
        future.set_exception(error)
        raise
    finally:
        with _pac_lock:_pac_pending.pop(key,None)


def _split_host_rule(rule):
    if rule.startswith("[") and "]" in rule:
        host,_,port=rule[1:].partition("]")
        return host,port.lstrip(":")
    if rule.count(":")==1:return rule.split(":",1)
    return rule,""


def _windows_bypass(url, rules):
    host=url.hostname
    port=str(url.port or (443 if url.scheme=="https" else 80))
    for rule in re.split(r"[;\s]+",rules.lower()):
        if not rule:continue
        if rule=="<local>":
            if "." not in host and ":" not in host:return True
            continue
        scheme, sep, rule_host=rule.partition("://")
        if sep and scheme!=url.scheme:continue
        pattern, rule_port=_split_host_rule(rule_host if sep else rule)
        if not rule_port or port==rule_port:
            if fnmatchcase(host,pattern):return True
    return False


def _configured_address(address, scheme="http"):
    value=address if "://" in address else scheme+"://"+address
    try:
        parsed=urlsplit(value)
        # WinHTTP permits a bare hostname. The settings input itself requires a port.
        if parsed.port is None:
            value=parsed._replace(netloc=parsed.netloc+(":1080" if parsed.scheme in ("socks5","socks5h") else ":80")).geturl()
        return proxy_address(value)
    except ValueError:
        raise RouteError("系统代理地址不受支持，请检查网络设置") from None


def _choose_windows_proxy(value, scheme):
    """Read a Windows manual protocol mapping or the primary configured proxy."""
    if not value:return ""
    if re.search(r"(?:^|[;\s])(?:https?|socks)=",value,re.I):
        mapping={}
        for item in re.split(r"[;\s]+",value.strip()):
            protocol,sep,address=item.partition("=")
            if sep:mapping.setdefault(protocol.lower(),address)
        for protocol in dict.fromkeys((scheme,"http","socks")):
            if mapping.get(protocol):
                return _configured_address(mapping[protocol],"socks5" if protocol=="socks" else "http")
        return ""
    first=value.split(";",1)[0].strip().split()
    if not first or first[0].upper()=="DIRECT":return ""
    return _configured_address(first[0])


def _fallback_system(url):
    proxies=getproxies()
    if proxy_bypass(url.netloc):
        return _system_route(source="系统绕过")
    for scheme in dict.fromkeys((url.scheme,"http","socks","all")):
        if proxies.get(scheme):
            return _system_route(_configured_address(proxies[scheme],"socks5" if scheme=="socks" else "http"))
    return _system_route()


def resolve_system_proxy(url):
    destination=urlsplit(url)
    if sys.platform!="win32":return _fallback_system(destination)
    try:config=_windows_config()
    except OSError:return _fallback_system(destination)
    auto,pac,proxies,bypass=config
    pac_failure=False
    if auto or pac:
        # Match Chromium's HTTPS PAC input. Paths and signed query strings stay private.
        target=(origin_url(url) if destination.scheme=="https"
                else destination._replace(netloc=destination.netloc.rsplit("@",1)[-1],fragment="").geturl())
        result=_pac_lookup(target,config)
        if isinstance(result,Route):return result
        pac_failure=bool(pac) or result!=AUTODETECTION_FAILED
    if proxies:
        source="PAC 失败，使用系统设置" if pac_failure else ""
        if _windows_bypass(destination,bypass):
            return _system_route(source="PAC 失败，使用系统绕过规则" if pac_failure else "系统绕过")
        proxy=_choose_windows_proxy(proxies,destination.scheme)
        if proxy:return _system_route(proxy,source=source)
    if pac_failure:raise RouteError("系统 PAC 解析失败，请检查网络设置")
    return _fallback_system(destination)


def resolve_route(mode,proxy,url):
    if mode=="system":return resolve_system_proxy(url)
    if mode=="direct":return Route("","直连（应用层）")
    if mode=="auto":return _auto_routes.plan(url,proxy)[1][0]
    if mode!="proxy":raise ValueError("连接方式无效")
    address=proxy_address(proxy)
    if not address:raise ValueError("请填写代理地址")
    return Route(address,"指定代理 · "+_proxy_label(address))


def origin_url(url):
    parts=urlsplit(url)
    return parts._replace(netloc=parts.netloc.rsplit("@",1)[-1],path="/",query="",fragment="").geturl()


def system_proxy():
    try:return resolve_system_proxy(API_PROBE).proxy
    except RouteError:return ""


def label(mode,proxy,url=None):
    if url is None and mode in ("system","auto"):return FILE_LABELS[mode]
    try:
        if mode=="auto":return _auto_routes.plan(url,proxy,probe=False)[1][0].label
        return resolve_route(mode,proxy,url).label
    except RouteError as error:return str(error)


def _recv_exact(sock,count):
    data=bytearray()
    while len(data)<count:
        block=sock.recv(count-len(data))
        if not block:raise OSError("SOCKS5 代理提前关闭了连接")
        data.extend(block)
    return data


def _socks_connect(proxy,address,timeout,source_address):
    host,port=address
    try:
        ip=ipaddress.ip_address(host)
        target=(b"\x01" if ip.version==4 else b"\x04")+ip.packed
    except ValueError:
        encoded=host.encode("idna")
        if not 0<len(encoded)<=255:raise OSError("SOCKS5 域名无效")
        target=b"\x03"+bytes([len(encoded)])+encoded
    sock=socket.create_connection(proxy,timeout,source_address)
    try:
        sock.sendall(b"\x05\x01\x00")
        if _recv_exact(sock,2)!=b"\x05\x00":raise OSError("SOCKS5 代理未接受无认证连接")
        sock.sendall(b"\x05\x01\x00"+target+port.to_bytes(2,"big"))
        version,status,reserved,kind=_recv_exact(sock,4)
        if version!=5 or reserved!=0 or status!=0:
            raise OSError(f"SOCKS5 连接失败 ({status})")
        count=_recv_exact(sock,1)[0] if kind==3 else {1:4,4:16}.get(kind)
        if count is None:raise OSError("SOCKS5 代理响应无效")
        _recv_exact(sock,count+2)
        return sock
    except BaseException:
        sock.close()
        raise


def connection(url,route,timeout,**kwargs):
    """Build an HTTP(S) connection. Use absolute-form targets for plain HTTP over an HTTP proxy."""
    target=urlsplit(url)
    proxy=urlsplit(route.proxy)
    cls=http.client.HTTPSConnection if target.scheme=="https" else http.client.HTTPConnection
    port=target.port or cls.default_port
    if proxy.scheme=="http":
        conn=cls(proxy.hostname,proxy.port,timeout=timeout,**kwargs)
        if target.scheme=="https":conn.set_tunnel(target.hostname,port)
        return conn
    conn=cls(target.hostname,port,timeout=timeout,**kwargs)
    if proxy.scheme=="socks5":
        conn._create_connection=partial(_socks_connect,(proxy.hostname,proxy.port))
    return conn


@dataclass
class _AutoChoice:
    preferred: str
    next_probe: float
    scores: dict = field(default_factory=dict)
    cooldown: dict = field(default_factory=dict)
    probing: bool = False


class _AutoRoutes:
    """Choose routes by verified TLS setup time with a short cooldown on transport failures."""

    def __init__(self):
        self.lock=Lock()
        self.choices=OrderedDict()
        self.active=0
        self.workers=ThreadPoolExecutor(max_workers=2,thread_name_prefix="FabRoute")

    def plan(self,url,proxy,*,probe=True):
        target=urlsplit(url)
        if target.scheme!="https" or not target.hostname or target.username is not None:
            raise RouteError("自动选路要求有效的 HTTPS 地址")
        host=f"[{target.hostname}]" if ":" in target.hostname else target.hostname
        origin=f"https://{host}"+(f":{target.port}" if target.port not in (None,443) else "")+"/"
        # Resolve the current system policy before consulting any previously measured routes.
        try:
            routes=[resolve_system_proxy(origin)]
        except RouteError:
            if not proxy:raise
            routes=[]
        else:
            routes.append(resolve_route("direct","",origin))
        if proxy:routes.insert(1,resolve_route("proxy",proxy,origin))
        distinct={}
        for route in routes:distinct.setdefault(route.signature,route)
        routes=tuple(Route(route.proxy,"自动选路 · "+route.label) for route in distinct.values())
        key=origin,tuple(route.signature for route in routes)
        now=time.monotonic()
        with self.lock:
            choice=self.choices.get(key)
            if choice is None:
                if not probe:return key,routes
                choice=self.choices[key]=_AutoChoice(routes[0].signature,now)
            self.choices.move_to_end(key)
            if len(self.choices)>128:self.choices.popitem(last=False)
            if probe and len(routes)>1 and now>=choice.next_probe and not choice.probing and self.active<2:
                choice.probing=True
                self.active+=1
                self.workers.submit(self._probe,key,choice,routes)
            available=[r for r in routes if choice.cooldown.get(r.signature,0)<=now]
            ordered=sorted(available or routes,key=lambda r:(r.signature!=choice.preferred,choice.scores.get(r.signature,float("inf"))))
        return key,tuple(ordered)

    def _probe(self,key,choice,routes):
        scores={}
        try:
            for route in routes:
                # TLS only. A signed URL, HTTP headers, cookies and body never enter a probe.
                conn=connection(key[0],route,1.5)
                start=time.monotonic()
                try:
                    conn.connect()
                    scores[route.signature]=time.monotonic()-start
                except OSError:pass
                finally:conn.close()
        finally:
            now=time.monotonic()
            with self.lock:
                if scores:
                    best=min(scores,key=scores.get)
                    current=scores.get(choice.preferred)
                    if current is None or scores[best]<current-max(current*0.20,0.035):
                        choice.preferred=best
                    choice.scores=scores
                choice.next_probe=now+90
                choice.probing=False
                self.active-=1

    def failed(self,key,route):
        with self.lock:
            if choice:=self.choices.get(key):
                choice.cooldown[route.signature]=time.monotonic()+25


_auto_routes=_AutoRoutes()
_RETRY_CONNECT_ERRORS={errno.ENETUNREACH,errno.EHOSTUNREACH,10051,10065}


def connect_automatic(url,proxy,timeout,**kwargs):
    """Connect before the HTTP request starts. At most one alternate can be tried."""
    key,routes=_auto_routes.plan(url,proxy)
    remaining=float(timeout)
    deadline=time.monotonic()+remaining
    for i,route in enumerate(routes[:2]):
        has_alternate=i==0 and len(routes)>1
        conn=connection(url,route,min(remaining,4) if has_alternate else remaining,**kwargs)
        try:
            conn.connect()
            conn.sock.settimeout(max(deadline-time.monotonic(),0.001))
            return conn,route
        except BaseException as error:
            conn.close()
            if not (isinstance(error,(ConnectionError,TimeoutError,socket.gaierror)) or
                    getattr(error,"errno",None) in _RETRY_CONNECT_ERRORS):
                raise
            _auto_routes.failed(key,route)
            remaining=deadline-time.monotonic()
            if not has_alternate or remaining<=0:raise


class _RoutedHTTP(HTTPHandler):
    def __init__(self, select):
        self.select=select
        super().__init__()

    def http_open(self,request):
        route=request.fab_route=self.select(request.full_url)
        if route.proxy.startswith("http://"):request.selector=request.full_url
        response=self.do_open(lambda host,timeout:connection(request.full_url,route,timeout),request)
        response.fab_route=route
        return response


class _RoutedHTTPS(HTTPSHandler):
    def __init__(self, select, auto_proxy=None):
        self.select=select
        self.auto_proxy=auto_proxy
        super().__init__()

    def https_open(self,request):
        def connect(host,timeout):
            if self.auto_proxy is not None:
                conn,route=connect_automatic(request.full_url,self.auto_proxy,timeout)
            else:
                route=self.select(request.full_url)
                conn=connection(request.full_url,route,timeout)
            request.fab_route=route
            return conn
        response=self.do_open(connect,request)
        response.fab_route=request.fab_route
        return response


class _WebRedirects(HTTPRedirectHandler):
    def __init__(self,secure_only=False):
        self.secure_only=secure_only
        super().__init__()

    def redirect_request(self,req,fp,code,msg,headers,newurl):
        scheme=urlsplit(newurl).scheme
        if scheme!="https" and (self.secure_only or scheme!="http"):
            raise HTTPError(req.full_url,code,"Unsupported redirect destination",headers,fp)
        return super().redirect_request(req,fp,code,msg,headers,newurl)


def opener(mode,proxy,target_url=None, *, initial_route=None):
    """Return a urllib-compatible sender that re-evaluates the route on redirects."""
    def select(url):
        if initial_route is not None and url==target_url:return initial_route
        return resolve_route(mode,proxy,url)
    handlers=(ProxyHandler({}),_RoutedHTTP(select),_RoutedHTTPS(select,proxy if mode=="auto" else None),
              _WebRedirects(secure_only=mode=="auto"))
    return build_opener(*handlers).open


def measure(proxy,cdn_url=None):
    limit=256*1024
    if cdn_url and urlsplit(cdn_url).scheme!="https":cdn_url=None
    modes=["system","direct"]+(["proxy"] if proxy else [])
    targets=[API_PROBE]+([cdn_url] if cdn_url else [])

    def plan(mode,url):
        try:return resolve_route(mode,proxy,url)
        except RouteError as error:return error

    def sender(mode,url,route):
        if isinstance(route,Route):return opener(mode,proxy,url,initial_route=route)
        def unavailable(*args,**kwargs):raise route
        return unavailable

    def probe(mode,routes):
        send=sender(mode,API_PROBE,routes[0])
        times=[]
        for _ in range(2):
            start=time.monotonic()
            try:
                with send(Request(API_PROBE,headers={"Accept":"application/json","User-Agent":"Mozilla/5.0"}),timeout=5) as res:
                    if b'"results"' not in res.read(32768):raise ValueError("unexpected response")
                times.append(round((time.monotonic()-start)*1000))
            except (OSError,ValueError,http.client.HTTPException):pass
        row=dict(ok=len(times),ms=statistics.median(times) if times else None,speed=None,first=None)
        if cdn_url:
            send=sender(mode,cdn_url,routes[1])
            start=time.monotonic()
            try:
                with send(Request(cdn_url,headers={"Range":f"bytes=0-{limit-1}","Accept-Encoding":"identity"}),timeout=6) as res:
                    data=res.read(1)
                    row["first"]=round((time.monotonic()-start)*1000)
                    n=len(data)+len(res.read(limit-1))
                    if n:row["speed"]=round(n/max(time.monotonic()-start,.001)/1024)
            except (OSError,ValueError,http.client.HTTPException):pass
        return row

    plans={mode:[plan(mode,url) for url in targets] for mode in modes}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures={}
        rows=[]
        for mode,routes in plans.items():
            # Same initial proxy does not imply identical routes after a redirect.
            key=(mode=="system",*(r.signature if isinstance(r,Route) else str(r) for r in routes))
            if key not in futures:futures[key]=pool.submit(probe,mode,routes)
            rows.append((mode,routes,futures[key]))
        rows=[dict(future.result(),mode=mode,routes=[r.label if isinstance(r,Route) else str(r) for r in routes])
              for mode,routes,future in rows]
    reliable=[r for r in rows if r["ok"] and not (sys.platform=="darwin" and r["mode"]=="direct")]
    fast=[r for r in rows if r["speed"]]
    return dict(rows=rows,api=min(reliable,key=lambda r:(-r["ok"],r["ms"]))["mode"] if reliable else None,
                files=max(fast,key=lambda r:r["speed"])["mode"] if fast else None,
                at=int(time.time()),cap=limit)
