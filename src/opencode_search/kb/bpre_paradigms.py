"""Non-C paradigm BPRE: list_lit / apply / message_expression / command.

P6/HR15 Part B: classifies purely via _V verb ground-truth, the URL-path anchor, and the
structural handler-shape/provenance discriminators shared with bpre_generic.py — no per-language
method-name table (the retired `spec.cli`/`spec.rte`)."""
from opencode_search.kb.valueflow import _t as _vt,_first_str as _fs
from opencode_search.kb.bpre_spec import _V
from opencode_search.kb.bpre_generic import _gsv,_provenance,_has_handler_arg
def _sc(n,b):return next((_vt(n.named_child(i),b) for i in range(n.named_child_count()) if n.named_child(i).kind()=="string_content"),None)
def scan_paradigm(n,b,f,surf,du,tu=None):
    """Bespoke BPRE extraction for list_lit/apply/message_expression/command."""
    k=n.kind();ps=surf.proto_services;ln=n.start_position().row+1
    if k=="list_lit":
        ch=[n.named_child(i) for i in range(n.named_child_count())]
        if not ch or ch[0].kind()!="sym_lit":return
        hd=ch[0];nm=hd.child_by_field_name("name")
        ns=next((hd.named_child(i) for i in range(hd.named_child_count()) if hd.named_child(i).kind()=="sym_ns"),None)
        ml=(_vt(nm,b) if nm else _vt(hd,b)).lower();rc=_vt(ns,b) if ns else None
        sv=_gsv(rc,ps) if rc else None
        p=next((_vt(c,b).strip("\"'") for c in ch[1:] if "str" in c.kind() or "string" in c.kind()),"")
        if sv:f.grpc_clients.append(("",sv,f"{rc}/{ml}",ln));return
        if not p.startswith("/"):return
        if ml in _V:(f.http_clients if rc else f.http_routes).append((ml.upper(),p,ln))
        elif _has_handler_arg(n):f.http_routes.append(("ANY",p,ln))
        elif rc and _provenance(rc,f.imports,tu):f.http_clients.append(("GET",p,ln))
    elif k in("apply","exp_apply"):
        fn=n.child_by_field_name("function");ml=_vt(fn,b).lower() if fn else ""
        p=_fs(n,b) or ""
        if p.startswith("/") and ml in _V:f.http_routes.append((ml.upper(),p,ln))
    elif k=="message_expression":
        rc=n.child_by_field_name("receiver");mth=n.child_by_field_name("method")
        if not rc or not mth:return
        ml=_vt(mth,b).lower().rstrip(":");sc=_sc(n,b)
        p=("/"+sc if sc and not sc.startswith("/") else (sc or (_fs(n,b) or "").lstrip("@\"'")))
        if not p.startswith("/"):return
        if ml in _V:f.http_clients.append((ml.upper(),p,ln))
        elif _provenance(_vt(rc,b),f.imports,tu):f.http_clients.append(("GET",p,ln))
    elif k=="command":
        ut=n.named_child(0) if n.named_child_count()>0 else None
        if not ut:return
        ch=[ut.named_child(i) for i in range(ut.named_child_count())]
        if len(ch)<2:return
        func=ch[1];mth=func.named_child(0) if func.named_child_count()>0 else None
        if not mth:return
        ml=_vt(mth,b).lower();p=_fs(func.named_child(1),b) if func.named_child_count()>1 else ""
        if not p or not p.startswith("/"):return
        if ml in _V:f.http_clients.append((ml.upper(),p,ln))
        elif _provenance(_vt(mth,b),f.imports,tu):f.http_clients.append(("GET",p,ln))
