"""Explicit route selection and bounded, read-only network measurements."""
import ipaddress
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener, getproxies, proxy_bypass

DEFAULTS=dict(language="zh-CN",apiRoute="system",fileRoute="system",proxy="",range="quixel",tier="personal",
              background=True,coverMiB=512)
LABELS={"system":"跟随系统","direct":"直连","proxy":"指定代理"}


def proxy_address(value):
    value=value.strip()
    if not value:return ""
    p=urlsplit(value if "://" in value else "http://"+value)
    if p.scheme!="http" or not p.hostname or not p.port or p.username or p.password or p.path not in ("","/") or p.query or p.fragment:
        raise ValueError("请输入 HTTP 代理的主机和端口，例如 127.0.0.1:7890")
    host=p.hostname
    try:
        ip=ipaddress.ip_address(host)
        host=f"[{ip}]" if ip.version==6 else str(ip)
    except ValueError:
        if not re.fullmatch(r"[a-z0-9.-]+",host,re.I):raise ValueError("代理主机名无效")
    return f"http://{host}:{p.port}"


def validate(values):
    s={k:values.get(k,v) for k,v in DEFAULTS.items()}
    if s["language"] not in ("en","zh-CN"):raise ValueError("界面语言无效")
    if any(s[k] not in LABELS for k in ("apiRoute","fileRoute")):raise ValueError("连接方式无效")
    s["proxy"]=proxy_address(s["proxy"])
    if "proxy" in (s["apiRoute"],s["fileRoute"]) and not s["proxy"]:raise ValueError("请填写代理地址")
    if s["range"] not in {"all","quixel"} or s["tier"] not in {"professional","personal"}:raise ValueError("默认许可或范围无效")
    if not isinstance(s["background"],bool) or s["coverMiB"] not in (128,256,512,1024,2048):raise ValueError("设置值无效")
    return s


def system_proxy():
    try:return proxy_address(getproxies().get("https",""))
    except ValueError:return ""


class ExplicitProxy(ProxyHandler):
    """Honor the route the user picked even when Windows has a bypass rule."""
    def proxy_open(self,request,proxy,scheme):
        request.set_proxy(urlsplit(proxy).netloc,"http")


def opener(mode,proxy):
    proxies=None if mode=="system" else {} if mode=="direct" else {"https":proxy,"http":proxy}
    handler=ExplicitProxy(proxies) if mode=="proxy" else ProxyHandler(proxies)
    return build_opener(handler).open


def label(mode,proxy,url=None):
    if mode=="direct":return "直连（应用层）"
    if mode=="proxy":return "指定代理 "+urlsplit(proxy).netloc
    detected=system_proxy()
    return "跟随系统 · "+(urlsplit(detected).netloc if detected and (url is None or not proxy_bypass(urlsplit(url).hostname)) else "无显式 HTTPS 代理")


def measure(proxy,cdn_url=None):
    api="https://www.fab.com/i/taxonomy/listing-types"
    limit=256*1024
    def probe(mode):
        send=opener(mode,proxy)
        times=[]
        for _ in range(2):
            start=time.monotonic()
            try:
                with send(Request(api,headers={"Accept":"application/json","User-Agent":"Mozilla/5.0"}),timeout=5) as res:
                    if b'"results"' not in res.read(32768):raise ValueError("unexpected response")
                times.append(round((time.monotonic()-start)*1000))
            except (OSError,ValueError):pass
        row=dict(mode=mode,ok=len(times),ms=statistics.median(times) if times else None,speed=None,first=None)
        if cdn_url:
            start=time.monotonic()
            try:
                with send(Request(cdn_url,headers={"Range":f"bytes=0-{limit-1}","Accept-Encoding":"identity"}),timeout=6) as res:
                    data=res.read(1)
                    row["first"]=round((time.monotonic()-start)*1000)
                    n=len(data)+len(res.read(limit-1))
                    if n:row["speed"]=round(n/max(time.monotonic()-start,.001)/1024)
            except (OSError,ValueError):pass
        return row
    modes=["system","direct"]+(["proxy"] if proxy else [])
    # Test identical configured HTTPS routes once, for both measured hosts.
    targets=[api]+([cdn_url] if cdn_url else [])
    def signature(mode):
        return tuple((system_proxy() if not proxy_bypass(urlsplit(u).hostname) else "") if mode=="system" else proxy if mode=="proxy" else "" for u in targets)
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures={}
        for mode in modes:
            key=signature(mode)
            if key not in futures:futures[key]=pool.submit(probe,mode)
        rows=[dict(futures[signature(m)].result(),mode=m) for m in modes]
    reliable=[r for r in rows if r["ok"]]
    fast=[r for r in rows if r["speed"]]
    return dict(rows=rows,api=min(reliable,key=lambda r:(-r["ok"],r["ms"]))["mode"] if reliable else None,
                files=max(fast,key=lambda r:r["speed"])["mode"] if fast else None,
                at=int(time.time()),cap=limit)




