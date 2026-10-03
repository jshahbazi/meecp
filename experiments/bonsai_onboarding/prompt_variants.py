"""User-authored adapter prompt template; rendered model inputs are never logged."""

USER_INVOKE_PROMPT = '''Write Python 3 source for the function `invoke(arguments, detect)`.
Output raw code only: no markdown fences, no explanation, no example usage, no docstrings.

Context:
- validate_arguments is a global function that exists before this code runs. Call it directly. Do not define it, import it, or add a fallback for it.
- detect is a callable passed in as the second parameter.

Implement these steps in order:
1. Create one io.StringIO() sink. Wrap steps 2 and 3 in
   `with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):`
   so stdout and stderr are restored automatically on every return or exception.
2. validated = validate_arguments(arguments)
   - except ValueError: return {'error': 'invalid_tool_arguments'}
   - except Exception: return {'error': 'app_execution_failed'}
3. result = detect(**validated)
   - except Exception: return {'error': 'app_execution_failed'}
4. If result is not an instance of dict: return {'error': 'invalid_tool_result'}
5. Return result unchanged.

Rules:
- Import only contextlib and io.
- Return values must never contain exception text or tracebacks.
- Do not modify arguments.
- Do not open files, use os.devnull, print, or log.'''
