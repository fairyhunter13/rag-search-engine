"""Grammar-agnostic BPRE extractor helpers for non-first-class languages."""
from __future__ import annotations
from opencode_search.kb.valueflow import _t as _vt, _first_str, resolve_first_arg
from opencode_search.kb.bpre_spec import _CALL_KINDS, _NEW_KINDS, _GRP_SFXS, _V, _is_call, _PARADIGM_KINDS, _HANDLER_KINDS

def _has_handler_arg(n, max_depth: int = 4) -> bool:
    """Structural discriminator: does this call carry a function/closure/lambda/block
    argument (a normal arg, a trailing block, or an attached lambda)? Node-kind only —
    no method-name vocabulary (HR15). True => route/handler registration shape;
    False => plain call (client or otherwise)."""
    stk = [(n, 0)]
    while stk:
        node, depth = stk.pop()
        if node.kind() in _HANDLER_KINDS:
            return True
        if depth < max_depth:
            stk.extend((node.named_child(i), depth + 1) for i in range(node.named_child_count()))
    return False

def _gsv(t,ps):
    s=t.rsplit(".",1)[-1].rsplit("::",1)[-1]
    return next((s[:-len(x)] for x in _GRP_SFXS if s.endswith(x) and len(s)>len(x) and s[:-len(x)] in ps),None)

def _ao(n):
    for fd in("arguments","argument_list"):
        a=n.child_by_field_name(fd)
        if a:return a
    cs=n.child_by_field_name("call_suffix")
    if cs:return cs.named_child(0) if cs.named_child_count()>0 else cs
    return next((n.named_child(i) for i in range(n.named_child_count()-1,-1,-1) if n.named_child(i).kind() in("arguments","argument_list","value_arguments","call_suffix")),None)

def _fs(n,b,du):
    a=_ao(n)
    if not a:return None
    v=_first_str(a,b)
    if v is not None:return v
    if a.named_child_count()==0:return None
    return _first_str(a.named_child(0),b) or resolve_first_arg(a,b,du)

def _cp(n,b):
    fn=n.child_by_field_name("function") or n.child_by_field_name("method") or n.child_by_field_name("name")
    rc=n.child_by_field_name("receiver") or n.child_by_field_name("object")
    if rc and fn:return _vt(rc,b),_vt(fn,b).rsplit(".",1)[-1].rsplit("::",1)[-1]
    if fn and n.named_child_count()>0 and n.named_child(0) is not fn:
        return _vt(n.named_child(0),b),_vt(fn,b).rsplit(".",1)[-1].rsplit("::",1)[-1]
    nd=fn or (n.named_child(0) if n.named_child_count()>0 else None)
    if not nd:return None,None
    t=_vt(nd,b)
    for s in("::","->","."):
        if s in t:p=t.rsplit(s,1);return p[0].strip(),p[1].strip()
    return None,t

def scan_generic(root,b,f,surface,du,spec):
    """Grammar-agnostic BPRE scanner driven by a _Spec (non-first-class languages)."""
    from opencode_search.kb.bpre_paradigms import scan_paradigm
    ps=surface.proto_services;stk=[root]
    while stk:
        n=stk.pop();k=n.kind();ln=n.start_position().row+1
        if k in _NEW_KINDS:
            tn=n.child_by_field_name("type") or n.child_by_field_name("class") or (n.named_child(0) if n.named_child_count()>0 else None)
            if tn and _gsv(_vt(tn,b),ps):f.grpc_clients.append(("",_gsv(_vt(tn,b),ps),f"new {_vt(tn,b)}",ln))
        elif k in _PARADIGM_KINDS:
            scan_paradigm(n,b,f,surface,du,spec);stk.extend(n.named_child(i) for i in range(n.named_child_count()-1,-1,-1));continue
        elif _is_call(k):
            rc,meth=_cp(n,b)
            if not meth:stk.extend(n.named_child(i) for i in range(n.named_child_count()-1,-1,-1));continue
            ml=meth.lower();sv=_gsv(rc,ps) if rc else None
            if sv and ml in spec.grp:f.grpc_clients.append(("",sv,f"{rc}.{meth}",ln));stk.extend(n.named_child(i) for i in range(n.named_child_count()-1,-1,-1));continue
            if not rc and meth[:1].isupper() and _gsv(meth,ps):f.grpc_clients.append(("",_gsv(meth,ps),meth,ln));stk.extend(n.named_child(i) for i in range(n.named_child_count()-1,-1,-1));continue
            p=(_fs(n,b,du) or "") if ml in _V or ml in spec.cli or ml in spec.rte else ""
            if ml in _V and p.startswith("/"):
                is_route=_has_handler_arg(n) if spec.structural else not rc
                (f.http_routes if is_route else f.http_clients).append((ml.upper(),p,ln))
            elif rc and ml in spec.cli:
                a=_ao(n)
                if a and a.named_child_count()>=2 and ml in("request","send","execute","perform"):
                    v2=_first_str(a.named_child(0),b) or "";p2=_first_str(a.named_child(1),b) or ""
                    if v2.upper() in{"GET","POST","PUT","PATCH","DELETE"} and p2.startswith("/"):f.http_clients.append((v2.upper(),p2,ln));stk.extend(n.named_child(i) for i in range(n.named_child_count()-1,-1,-1));continue
                if p.startswith("/"):f.http_clients.append(("GET",p,ln))
            elif ml in spec.rte and p.startswith("/"):f.http_routes.append(("ANY",p,ln))
        elif k in("annotation","attribute","attribute_item","meta","meta_item") and spec.dec:
            nm=n.child_by_field_name("name")
            if nm:
                ann=_vt(nm,b).lower()
                if ann in spec.dec:
                    an=n.child_by_field_name("arguments");p=(_first_str(an.named_child(0),b) if an and an.named_child_count()>0 else None) or ""
                    if p.startswith("/"):f.http_routes.append((next((v for v in("get","post","put","patch","delete") if v in ann),"any").upper(),p,ln))
            inner=n.named_child(0) if n.named_child_count()>0 else None
            if inner and inner.kind() in _CALL_KINDS:
                _,m2=_cp(inner,b)
                if m2 and m2.lower() in spec.dec:
                    p=_fs(inner,b,du) or ""
                    if p.startswith("/"):f.http_routes.append((m2.lower().upper() if m2.lower() in _V else "ANY",p,ln))
        stk.extend(n.named_child(i) for i in range(n.named_child_count()-1,-1,-1))
