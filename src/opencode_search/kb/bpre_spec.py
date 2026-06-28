"""Declarative per-language spec registry for BPRE generic engine."""
from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class _Spec:
    cli:frozenset=frozenset()
    rte:frozenset=frozenset()
    dec:frozenset=frozenset()
    grp:frozenset=frozenset()

_V=frozenset({"get","post","put","patch","delete","head","options"})
_CALL_KINDS=frozenset({"call","call_expression","invocation_expression","method_invocation","function_call_expression","member_call_expression","scoped_call_expression","application","method_call","send","funcall","method_call_expression"})
_NEW_KINDS=frozenset({"object_creation_expression","new_expression","instance_creation_expression","class_instance_creation_expression","constructor_invocation"})
_FIRST_CLASS=frozenset({"go","python","typescript","javascript","php"})
_GRP_SFXS=("ServiceClient","BlockingStub","FutureStub","AsyncStub","Stub","Client","Grpc")
_LANG_SPECS:dict[str,_Spec]={
    "ruby":_Spec(cli=frozenset({"request","perform","execute"}),rte=frozenset({"route","resources","match","root","scope"}),grp=frozenset({"new"})),
    "c_sharp":_Spec(cli=frozenset({"getasync","postasync","putasync","patchasync","deleteasync","sendasync","getstringasync","getfromjsonasync","postasjsonasync","send","getforobject","exchange","getstring"}),rte=frozenset({"mapget","mappost","mapput","mappatch","mapdelete","map"}),dec=frozenset({"httpget","httppost","httpput","httppatch","httpdelete","route"}),grp=frozenset({"new","create"})),
    "rust":_Spec(cli=frozenset({"request","execute","send","fetch","call","text","json"}),rte=frozenset({"route","nest","on","handle"}),dec=_V|frozenset({"route","any"}),grp=frozenset({"new","connect","new_client","with_origin"})),
    "elixir":_Spec(cli=frozenset({"request","call","send_request","request!","get!","post!","put!","patch!","delete!"}),rte=frozenset({"resources","scope","match","live","forward","pipe_through"}),grp=frozenset({"new","channel","stub","connect"})),
    "java":_Spec(cli=frozenset({"getforobject","getforentity","postforobject","postforentity","exchange","retrieve","send","getstring"}),dec=frozenset({"getmapping","postmapping","putmapping","patchmapping","deletemapping","requestmapping"}),grp=frozenset({"newblockingstub","newstub","newfuturestub","create"})),
    "kotlin":_Spec(cli=frozenset({"getforobject","getforentity","postforobject","exchange","retrieve","send","fetch"}),dec=frozenset({"getmapping","postmapping","putmapping","patchmapping","deletemapping","requestmapping"}),grp=frozenset({"newblockingstub","newstub","newfuturestub","new"})),
    "scala":_Spec(cli=frozenset({"request","send","execute","run","ask","singlerequest"}),rte=frozenset({"route","path","concat","nest","prefix"}),grp=frozenset({"newblockingstub","newstub","apply","new","stub"})),
    "swift":_Spec(cli=frozenset({"data","datatask","upload","request","send","perform","download","response"}),rte=frozenset({"on","group","grouped","middleware"}),dec=_V|frozenset({"route"}),grp=frozenset({"init","create","makeclient"})),
    "dart":_Spec(cli=frozenset({"request","send","read","fetch","call"}),rte=frozenset({"route","add","mount","handler"}),grp=frozenset({"new","create","connect"})),
    "cpp":_Spec(cli=frozenset({"send","execute","perform","getasync","postasync","sendasync","request"}),rte=frozenset({"route","addroute","sethandler"}),grp=frozenset({"newstub","create","new_stub","make_stub"})),
}
_DEFAULT_SPEC=_Spec(cli=frozenset({"request","fetch","send","execute","perform"}),rte=frozenset({"route","match","map","resources","any"}),grp=frozenset({"new","connect","create"}))
