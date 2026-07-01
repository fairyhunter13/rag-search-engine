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
from opencode_search.kb.valueflow import build_def_use, build_type_use, resolve_first_arg, _t as _vt
from opencode_search.kb.bpre_spec import _FIRST_CLASS, _IMPORT_KINDS
from opencode_search.kb.bpre_generic import scan_generic
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
    imports:dict=field(default_factory=dict)  # alias -> import/module path (P6/HR15 Part C1)
@dataclass
class ApiSurface:
    constructors:dict=field(default_factory=dict)
    registrars:dict=field(default_factory=dict)
    methods:dict=field(default_factory=dict)
    proto_import_paths:set=field(default_factory=set)
    pubsub_import_paths:set=field(default_factory=set)
    proto_services:set=field(default_factory=set)  # service names from .proto files

_PHP_STR=frozenset({"string","encapsed_string"})  # PHP string literal node kinds
_HTTP_VERBS=frozenset({"get","post","put","patch","delete","options","head","request","any","match"})
def _t(n,b):r=n.byte_range();return b[r.start:r.end].decode("utf-8","replace")
def _s1(a,b):return next((_t(a.named_child(i),b).strip("\"'`") for i in range(a.named_child_count()) if a.named_child(i).kind() in("interpreted_string_literal","raw_string_literal","string_literal")),None)
def _qt(n,b):p,nm=n.child_by_field_name("package"),n.child_by_field_name("name");return(_t(p,b),_t(nm,b)) if n.kind()=="qualified_type" and p and nm else None
def _pk(n,b,pa):
    if n.kind()=="unary_expression":n=n.child_by_field_name("operand") or n
    if n.kind()!="composite_literal":return None
    tn=n.child_by_field_name("type");r=_qt(tn,b) if tn else None
    return f"{r[0]}.{r[1]}" if r and r[0] in pa else None
def _ss(fn,px,*sx):n=fn[len(px):];return next((n[:-len(s)] for s in sx if n.endswith(s)),n)

def _php_str(node,b:bytes,du:dict)->str|None:
    """Get a PHP expression's string value: literal or def-use lookup."""
    if node is None:return None
    if node.kind()=="argument" and node.named_child_count()>0:node=node.named_child(0)
    if node.kind() in _PHP_STR:return _vt(node,b).strip("'\"")
    if node.kind()=="variable_name":return du.get(_vt(node,b))
    return None

_IMPORT_WRAP = "\"'`(){}<>;, "

def _import_path_alias(n, b: bytes) -> list[tuple[str, str]]:
    """Text-based (structural, no-regex) alias/path extraction for one _IMPORT_KINDS node.

    Mirrors the already-accepted Go pattern (bpre_ast.py:159, `ip.rsplit("/",1)[-1]`):
    strips the leading declaration keyword, honours an explicit ` as `/`=` alias if present,
    else defaults the alias to the last path-separator segment. Never produces a wrong
    (alias, path) pair — only sometimes fails to find one, which the caller tolerates."""
    txt = _t(n, b).strip().rstrip(";").strip()
    parts = txt.split(None, 1)
    rest = parts[1].strip() if len(parts) == 2 else txt
    if not rest:
        return []
    if " as " in rest:
        path_part, alias_part = rest.rsplit(" as ", 1)
        path, alias = path_part.strip(_IMPORT_WRAP), alias_part.strip(_IMPORT_WRAP)
        return [(alias, path)] if alias and path else []
    if "=" in rest:
        alias_part, path_part = rest.split("=", 1)
        alias, path = alias_part.strip(_IMPORT_WRAP), path_part.strip(_IMPORT_WRAP)
        if alias and path and all(c.isalnum() or c == "_" for c in alias):
            return [(alias, path)]
    path = rest.strip(_IMPORT_WRAP)
    seg = path
    for sep in ("::", "/", "."):
        if sep in seg:
            seg = seg.rsplit(sep, 1)[-1]
    alias = seg.strip(_IMPORT_WRAP)
    return [(alias, path)] if alias and path else []

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

def _scan_imports(root, b: bytes) -> dict:
    """Universal import/use-declaration pre-pass (P6/HR15 Part C1): alias -> module-path, for
    every _IMPORT_KINDS node kind. First declaration wins (conservative, mirrors build_def_use).
    Go is excluded (already has its own richer import handling in scan_file below)."""
    imports: dict = {}
    stk = [root]
    while stk:
        n = stk.pop()
        if n.kind() in _IMPORT_KINDS:
            for alias, path in _import_path_alias(n, b):
                if alias not in imports:
                    imports[alias] = path
        stk.extend(n.named_child(i) for i in range(n.named_child_count() - 1, -1, -1))
    return imports

def _discover_proto_services(members:list[str],surf:ApiSurface)->None:
    """Seed surf.proto_services from .proto service declarations (language-neutral)."""
    from opencode_search.core.config import IGNORED_DIRS
    for member in members:
        for dp,dirs,fs in os.walk(member):
            dirs[:]=[d for d in dirs if d not in IGNORED_DIRS]
            for fname in fs:
                if not fname.endswith(".proto"):continue
                try:
                    for line in (Path(dp)/fname).read_text(errors="replace").splitlines():
                        parts=line.strip().split()
                        if len(parts)>=2 and parts[0]=="service":
                            svc=parts[1].rstrip("{").strip()
                            if svc:surf.proto_services.add(svc)
                except OSError:pass

def _scan_pb_go_file(fp_str:str)->dict:
    """Worker-executed (bounded_parse): parse one .pb.go file, return picklable contributions."""
    out={"constructors":{},"registrars":{},"methods":{},"proto_import_paths":[],"pubsub_import_paths":[]}
    try:
        c=Path(fp_str).read_text(errors="replace");root=_ts_api.get_parser("go").parse(c).root_node()
    except Exception:
        return out
    b=c.encode("utf-8","replace")
    for i in range(root.named_child_count()):
        nd=root.named_child(i)
        if nd.kind()=="function_declaration":
            nn=nd.child_by_field_name("name")
            if nn:
                fn=_t(nn,b)
                if fn.startswith("New") and fn.endswith("Client"):
                    sv=_ss(fn,"New","Client")
                    if sv:out["constructors"][fn]=sv
                elif fn.startswith("Register") and fn.endswith("Server"):
                    sv=_ss(fn,"Register","Server")
                    if sv:out["registrars"][fn]=sv
        elif nd.kind()=="method_declaration":
            recv=nd.child_by_field_name("receiver");nm=nd.child_by_field_name("name")
            if recv and nm:
                fn=_t(nm,b)
                if fn and fn[0].isupper():
                    for ri in range(recv.named_child_count()):
                        rp=recv.named_child(ri)
                        if rp.kind()!="parameter_declaration":continue
                        for ti in range(rp.named_child_count()):
                            tp=rp.named_child(ti);actual=tp
                            if tp.kind()=="pointer_type" and tp.named_child_count()>0:actual=tp.named_child(0)
                            if actual.kind() in("type_identifier","identifier"):
                                rt=_t(actual,b)
                                if rt.endswith("Client") and len(rt)>6 and len(fn)>=9:
                                    sv=rt[:-6];out["methods"].setdefault(fn,sv[0].upper()+sv[1:])
        elif nd.kind()=="import_declaration":
            for j in range(nd.named_child_count()):
                sp=nd.named_child(j)
                if sp.kind()!="import_spec":continue
                pn=sp.child_by_field_name("path")
                if not pn:continue
                p=_t(pn,b).strip("\"'`");out["proto_import_paths"].append(p)
                if "pubsub" in p:out["pubsub_import_paths"].append(p)
    return out

def federation_discover(members:list[str])->ApiSurface:
    surf=ApiSurface()
    _discover_proto_services(members,surf)
    if not _ts_has_language("go"):return surf
    from opencode_search.core.config import IGNORED_DIRS
    from opencode_search.core.index_config import effective_config, is_excluded
    from opencode_search.index.bounded_parse import PARSE_TIMEOUT, run_bounded
    for member in members:
        _mcfg=effective_config(Path(member))
        for dp,dirs,fs in os.walk(member):
            dirs[:]=[d for d in dirs if d not in IGNORED_DIRS]
            for fname in fs:
                if not fname.endswith(".pb.go"):continue
                _fp=Path(dp)/fname
                if _mcfg.exclude and is_excluded(_fp,_mcfg.exclude,Path(member)):continue
                contrib=run_bounded(_scan_pb_go_file,(str(_fp),),path_for_log=str(_fp))
                if not contrib or contrib==PARSE_TIMEOUT:continue
                surf.constructors.update(contrib["constructors"])
                surf.registrars.update(contrib["registrars"])
                surf.methods.update(contrib["methods"])
                surf.proto_import_paths.update(contrib["proto_import_paths"])
                surf.pubsub_import_paths.update(contrib["pubsub_import_paths"])
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
                        elif meth in s.registrars:f.grpc_servers.append((s.registrars[meth],ln))
                        elif meth in s.methods and "." in base:f.grpc_clients.append((base,s.methods[meth],meth,ln))
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
                    elif fn_n.kind()=="attribute" and base.lower() in _HTTP_VERBS:
                        # requests.get('/path'), httpx.post('/path') — plain call = HTTP client
                        p=_s1_or_vf(an,b,du)
                        if p and p.startswith("/"):f.http_clients.append((base.upper(),p,ln))
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
                            if p and p.startswith("/"):
                                f.http_routes.append((meth.upper(),p,ln))
                                if meth.lower() in _HTTP_VERBS:
                                    f.http_clients.append((meth.upper(),p,ln))
                    elif fn_n.kind()=="identifier" and _vt(fn_n,b)=="fetch":
                        p=_s1_or_vf(an,b,du)
                        if p and p.startswith("/"):f.http_clients.append(("GET",p,ln))
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
    elif lang=="php":
        stk=[root]
        while stk:
            n=stk.pop();k=n.kind()
            if k in("member_call_expression","scoped_call_expression"):
                nm_node=n.child_by_field_name("name");args_node=n.child_by_field_name("arguments")
                if nm_node and args_node:
                    meth=_vt(nm_node,b).lower();ln=n.start_position().row+1;nargs=args_node.named_child_count()
                    if meth=="request" and nargs>=2:
                        # $client->request('POST', '/path') — verb=arg0, path=arg1
                        v=_php_str(args_node.named_child(0),b,du)
                        p=_php_str(args_node.named_child(1),b,du)
                        if v and p and p.startswith("/"):f.http_clients.append((v.upper(),p,ln))
                    elif meth in _HTTP_VERBS and nargs>=1:
                        p=_php_str(args_node.named_child(0),b,du)
                        if p and p.startswith("/"):
                            if k=="scoped_call_expression":
                                f.http_routes.append((meth.upper(),p,ln))
                            else:
                                f.http_clients.append((meth.upper(),p,ln))
            elif k=="object_creation_expression":
                # PHP: class name is first named child (no field name in this grammar)
                if n.named_child_count()>0:
                    cls_name=_vt(n.named_child(0),b);ln=n.start_position().row+1
                    if cls_name.endswith("Client") and cls_name[:-6] in s.proto_services:
                        f.grpc_clients.append(("",cls_name[:-6],f"new {cls_name}",ln))
            stk.extend(n.named_child(i) for i in range(n.named_child_count()-1,-1,-1))
    if lang not in _FIRST_CLASS:
        f.imports = _scan_imports(root, b)
        tu = build_type_use(root, b)
        scan_generic(root, b, f, s, du, tu)
    return f
