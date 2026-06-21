"""BPRE Tier-1: Pass A discovers gRPC surface from *.pb.go; Pass B detects per file. No regex.

Tier-1.5 (value-flow): non-literal call arguments (const/var/field) are
resolved through per-file def-use maps via kb.valueflow before falling back to
the GPU-rank and LLM tiers.  Scanner extended to Python / TS / JS.
"""
from __future__ import annotations
import logging,os
from dataclasses import dataclass,field
from pathlib import Path
log=logging.getLogger(__name__)
from tree_sitter_language_pack import has_language as _ts_has_language, api as _ts_api
from opencode_search.kb.valueflow import build_def_use, resolve_first_arg, _t as _vt
@dataclass
class FileFacts:
    path:str
    grpc_clients:list=field(default_factory=list)
    grpc_servers:list=field(default_factory=list)
    proto_imports:dict=field(default_factory=dict)
    pubsub_imports:dict=field(default_factory=dict)
    pubsub_message_lines:list=field(default_factory=list)
    proto_marshal_types:list=field(default_factory=list)
    has_receive_call:bool=False
    pubsub_consumes:list=field(default_factory=list)
    http_routes:list=field(default_factory=list)
    http_clients:list=field(default_factory=list)
    status_enums:list=field(default_factory=list)
@dataclass
class ApiSurface:
    constructors:dict=field(default_factory=dict)
    registrars:dict=field(default_factory=dict)
    proto_import_paths:set=field(default_factory=set)
    pubsub_import_paths:set=field(default_factory=set)
def _t(n,b):r=n.byte_range();return b[r.start:r.end].decode("utf-8","replace")
def _s1(a,b):return next((_t(a.named_child(i),b).strip("\"'`") for i in range(a.named_child_count()) if a.named_child(i).kind() in("interpreted_string_literal","raw_string_literal","string_literal")),None)
def _qt(n,b):p,nm=n.child_by_field_name("package"),n.child_by_field_name("name");return(_t(p,b),_t(nm,b)) if n.kind()=="qualified_type" and p and nm else None
def _pk(n,b,pa):
    if n.kind()=="unary_expression":n=n.child_by_field_name("operand") or n
    if n.kind()!="composite_literal":return None
    tn=n.child_by_field_name("type");r=_qt(tn,b) if tn else None
    return f"{r[0]}.{r[1]}" if r and r[0] in pa else None
def _ss(fn,px,*sx):n=fn[len(px):];return next((n[:-len(s)] for s in sx if n.endswith(s)),n)

def _s1_or_vf(args, b: bytes, du: dict) -> str | None:
    """Get a string value from first arg: literal (_s1 fast path) OR value-flow lookup.

    Tier-1.5(a): resolves dynamic routes/topics that _s1 misses.
    """
    # Fast path: string literal (original _s1 behaviour)
    v = _s1(args, b)
    if v is not None:
        return v
    # Value-flow: try first named child as identifier / selector
    return resolve_first_arg(args, b, du)

def federation_discover(members:list[str])->ApiSurface:
    surf=ApiSurface()
    if not _ts_has_language("go"):return surf
    try:parser=_ts_api.get_parser("go")
    except Exception as e:log.warning("bpre_ast A: %s",e);return surf
    from opencode_search.core.config import IGNORED_DIRS
    for member in members:
        for dp,dirs,fs in os.walk(member):
            dirs[:]=[d for d in dirs if d not in IGNORED_DIRS]
            for fname in fs:
                if not fname.endswith(".pb.go"):continue
                try:c=(Path(dp)/fname).read_text(errors="replace");root=parser.parse(c).root_node()
                except Exception:continue
                b=c.encode("utf-8","replace")
                for i in range(root.named_child_count()):
                    nd=root.named_child(i)
                    if nd.kind()=="function_declaration":
                        nn=nd.child_by_field_name("name")
                        if nn:
                            fn=_t(nn,b)
                            if fn.startswith("New") and fn.endswith("Client"):
                                sv=_ss(fn,"New","ServiceClient","Client")
                                if sv:surf.constructors[fn]=sv
                            elif fn.startswith("Register") and fn.endswith("Server"):
                                sv=_ss(fn,"Register","Server")
                                if sv:surf.registrars[fn]=sv
                    elif nd.kind()=="import_declaration":
                        for j in range(nd.named_child_count()):
                            sp=nd.named_child(j)
                            if sp.kind()!="import_spec":continue
                            pn=sp.child_by_field_name("path")
                            if not pn:continue
                            p=_t(pn,b).strip("\"'`");surf.proto_import_paths.add(p)
                            if "pubsub" in p:surf.pubsub_import_paths.add(p)
    log.debug("bpre_ast A: %d ctors %d regs",len(surf.constructors),len(surf.registrars));return surf
def scan_file(path:str,content:str,lang:str,surface:ApiSurface)->FileFacts|None:
    if not _ts_has_language(lang):return None
    try:root=_ts_api.get_parser(lang).parse(content).root_node()
    except Exception:return None
    b=content.encode("utf-8","replace");f=FileFacts(path=path);s=surface
    # Tier-1.5(a): pre-pass builds file-scope def-use map for value-flow resolution.
    du=build_def_use(root,b)
    if lang=="go":
        pa:dict={};pubs:dict={}
        for i in range(root.named_child_count()):
            nd=root.named_child(i)
            if nd.kind()!="import_declaration":continue
            for j in range(nd.named_child_count()):
                sp=nd.named_child(j)
                if sp.kind()!="import_spec":continue
                pn=sp.child_by_field_name("path");nn=sp.child_by_field_name("name")
                if not pn:continue
                ip=_t(pn,b).strip("\"'`");alias=_t(nn,b) if nn else ip.rsplit("/",1)[-1]
                pa[alias]=ip
                if "pubsub" in ip or ip in s.pubsub_import_paths:pubs[alias]=ip
        f.proto_imports,f.pubsub_imports=pa,pubs
        stk=[root]
        while stk:
            n=stk.pop();k=n.kind()
            if k=="call_expression":
                fn=n.child_by_field_name("function");args=n.child_by_field_name("arguments");ln=n.start_position().row+1
                if fn and fn.kind()=="selector_expression":
                    op=fn.child_by_field_name("operand");fd=fn.child_by_field_name("field")
                    if op and fd:
                        base,meth=_t(op,b),_t(fd,b)
                        if meth in s.constructors:f.grpc_clients.append((base,s.constructors[meth],meth,ln))
                        elif base=="proto" and meth=="Marshal" and args and args.named_child_count()>0:
                            tk=_pk(args.named_child(0),b,pa)
                            if tk:f.proto_marshal_types.append((tk,ln))
                        elif base=="proto" and meth=="Unmarshal" and args and args.named_child_count()>1:
                            tk=_pk(args.named_child(1),b,pa)
                            if tk:f.pubsub_consumes.append((tk,ln))
                        elif meth=="Receive":f.has_receive_call=True
                        elif args:
                            p=_s1_or_vf(args,b,du)
                            if p and p.startswith("/"):f.http_routes.append((meth.upper(),p,ln))
                            elif base in pa and "http" in pa[base]:
                                if meth=="NewRequest":
                                    sv=[_t(args.named_child(i),b).strip("\"'`") for i in range(args.named_child_count()) if args.named_child(i).kind() in("interpreted_string_literal","raw_string_literal","string_literal")]
                                    if len(sv)>=2:f.http_clients.append((sv[0].upper(),sv[1],ln))
                                elif p:f.http_clients.append((meth.upper(),p,ln))
                elif fn and fn.kind()=="identifier":
                    nm=_t(fn,b)
                    if nm in s.registrars:f.grpc_servers.append((s.registrars[nm],ln))
            elif k=="composite_literal":
                tn=n.child_by_field_name("type");r=_qt(tn,b) if tn else None
                if r and r[0] in pubs and r[1]=="Message":f.pubsub_message_lines.append(n.start_position().row+1)
            elif k=="const_spec":
                nn,vn=n.child_by_field_name("name"),n.child_by_field_name("value")
                if nn and vn:
                    for i in range(vn.named_child_count()):
                        c=vn.named_child(i)
                        if c.kind() in("interpreted_string_literal","raw_string_literal"):
                            v=_t(c,b).strip("\"'`")
                            if v:f.status_enums.append(v)
            stk.extend(n.named_child(i) for i in range(n.named_child_count()-1,-1,-1))
    elif lang in("java","kotlin"):
        stk=[root]
        while stk:
            n=stk.pop()
            if n.kind()=="annotation":
                nn=n.child_by_field_name("name")
                if nn:
                    ann=_t(nn,b)
                    if ann.endswith("Mapping"):
                        an=n.child_by_field_name("arguments")
                        verb=None
                        if an:
                            at=_t(an,b)
                            if "RequestMethod." in at:
                                idx=at.find("RequestMethod.")+len("RequestMethod.")
                                verb=at[idx:].split(")")[0].split(",")[0].strip().rstrip("}").strip().upper()
                        if not verb:
                            prefix=ann[:-len("Mapping")]
                            verb=prefix.upper() if prefix else "GET"
                        if an:
                            p=_s1_or_vf(an,b,du)
                            if p:f.http_routes.append((verb,p,n.start_position().row+1))
            stk.extend(n.named_child(i) for i in range(n.named_child_count()-1,-1,-1))
    elif lang=="python":
        # HTTP: decorators with "/" arg (FastAPI/Flask/aiohttp — structural, not vocab)
        # gRPC: constructor/registrar names matched against surf (proto FQN discovery)
        stk=[root]
        while stk:
            n=stk.pop();k=n.kind()
            if k=="decorator":
                inner=n.named_child(0) if n.named_child_count()>0 else None
                if inner and inner.kind()=="call":
                    fn_n=inner.child_by_field_name("function");an=inner.child_by_field_name("arguments")
                    if fn_n and an:
                        meth=""
                        if fn_n.kind()=="attribute":
                            attr=fn_n.child_by_field_name("attribute")
                            if attr:meth=_vt(attr,b)
                        elif fn_n.kind()=="identifier":meth=_vt(fn_n,b)
                        p=_s1_or_vf(an,b,du)
                        if p and p.startswith("/"):
                            f.http_routes.append((meth.upper() if meth else "ANY",p,n.start_position().row+1))
            elif k=="call":
                fn_n=n.child_by_field_name("function");an=n.child_by_field_name("arguments")
                ln=n.start_position().row+1
                if fn_n and an:
                    fn_txt=_vt(fn_n,b)
                    base=fn_txt.rsplit(".",1)[-1] if "." in fn_txt else fn_txt
                    if base in s.constructors:f.grpc_clients.append(("",s.constructors[base],base,ln))
                    elif base in s.registrars:f.grpc_servers.append((s.registrars[base],ln))
            stk.extend(n.named_child(i) for i in range(n.named_child_count()-1,-1,-1))
    elif lang in("typescript","javascript"):
        # HTTP: app.get('/path', h) or @Get('/path') decorators (structural — no keyword list)
        stk=[root]
        while stk:
            n=stk.pop();k=n.kind()
            if k=="call_expression":
                fn_n=n.child_by_field_name("function");an=n.child_by_field_name("arguments")
                ln=n.start_position().row+1
                if fn_n and an:
                    if fn_n.kind()=="member_expression":
                        prop=fn_n.child_by_field_name("property")
                        if prop:
                            meth=_vt(prop,b);p=_s1_or_vf(an,b,du)
                            if p and p.startswith("/"):f.http_routes.append((meth.upper(),p,ln))
                    fn_txt=_vt(fn_n,b)
                    base=fn_txt.rsplit(".",1)[-1] if "." in fn_txt else fn_txt
                    if base in s.constructors:f.grpc_clients.append(("",s.constructors[base],base,ln))
                    elif base in s.registrars:f.grpc_servers.append((s.registrars[base],ln))
            elif k=="decorator":
                inner=n.named_child(0) if n.named_child_count()>0 else None
                if inner and inner.kind()=="call_expression":
                    fn_n=inner.child_by_field_name("function");an=inner.child_by_field_name("arguments")
                    if fn_n and an:
                        p=_s1_or_vf(an,b,du)
                        if p and p.startswith("/"):
                            f.http_routes.append((_vt(fn_n,b).upper(),p,n.start_position().row+1))
            stk.extend(n.named_child(i) for i in range(n.named_child_count()-1,-1,-1))
    return f
