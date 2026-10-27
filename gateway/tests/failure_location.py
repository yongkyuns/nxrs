"""Bounded public source identities for owned fixtures, without exception text."""
CLASSES=frozenset(['AssertionError','ValueError','TypeError','KeyError','OSError','FileNotFoundError',
    'PermissionError','ProcessLookupError','TimeoutError','RuntimeError','TimeoutExpired',
    'ModuleNotFoundError','ImportError','URLError','HTTPError','CancelledError','Exception'])


def project_locations(value,accepted_sources):
    if not isinstance(value,list) or len(value)>8:return []
    result=[]
    for item in value:
        if not isinstance(item,dict):continue
        source=item.get('source');kind=item.get('error');line=item.get('line')
        if (isinstance(source,str) and source in accepted_sources and isinstance(kind,str) and kind in CLASSES
                and type(line) is int and 1<=line<=10000):
            result.append(dict(source=source,line=line,error=kind))
    return result


def trace_locations(error,sources):
    # Callers supply exact code-file identities and fixed public repository names.
    # Never read source text, locals, arguments, exception messages or path values.
    result=[];seen=set()
    for _ in range(4):
        if error is None or id(error) in seen:break
        seen.add(id(error));kind=type(error).__name__
        if kind not in CLASSES:kind='Exception'
        trace=error.__traceback__
        for _ in range(32):
            if trace is None or len(result)>=8:break
            name=sources.get(trace.tb_frame.f_code.co_filename)
            if name and 1<=trace.tb_lineno<=10000:
                value=dict(source=name,line=trace.tb_lineno,error=kind)
                if value not in result:result.append(value)
            trace=trace.tb_next
        error=error.__cause__ or error.__context__
    return result
