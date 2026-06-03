"""
code_executor.py — Enhanced executor with performance tracking,
undo support, and multi-stage execution (model → drawing).
"""

import adsk.core
import adsk.fusion
import traceback
import time


class CodeExecutor:
    """Executes AI-generated Fusion 360 code with safety and performance."""

    ALLOWED_MODULES = {
        'adsk', 'adsk.core', 'adsk.fusion', 'adsk.cam', 'adsk.drawing',
        'math', 'collections', 'itertools', 'functools', 'json', 're'
    }

    BLOCKED_TOKENS = [
        'os.system', 'subprocess', 'shutil', 'eval(', '__import__',
        'exec(', 'open(', 'pathlib', 'socket', 'http', 'urllib',
        'requests', 'importlib', 'ctypes', 'sys.exit',
        'documents.add', 'app.quit',
    ]

    def __init__(self, app: adsk.core.Application):
        self.app = app
        self.design = adsk.fusion.Design.cast(app.activeProduct)
        self.root_comp = self.design.rootComponent
        self.perf_log = []

    def execute_model(self, code: str, config: dict = None) -> dict:
        """
        Execute the build() function.
        Returns: {success, message, bodies, params, features, exec_time_ms}
        """
        result = {
            'success': False, 'message': '', 'bodies': [],
            'params': {}, 'features': [], 'exec_time_ms': 0,
            'component': None
        }

        # ── Validate ──
        is_safe, reason = self._validate(code)
        if not is_safe:
            result['message'] = f'Security: {reason}'
            return result

        # ── Compile ──
        try:
            compiled = compile(code, '<TextToCAD_model>', 'exec')
        except SyntaxError as e:
            result['message'] = f'Syntax error: {e}'
            return result

        # ── Track timeline for undo ──
        timeline = self.design.timeline
        start_marker = timeline.count

        # ── Execute ──
        namespace = self._make_namespace()
        try:
            exec(compiled, namespace)
        except Exception as e:
            result['message'] = f'Definition error: {e}'
            return result

        build_fn = namespace.get('build')
        if not callable(build_fn):
            result['message'] = 'No build() function found.'
            return result

        t0 = time.time()
        try:
            build_result = build_fn(self.root_comp, config or {})
            adsk.doEvents()
            self.app.activeViewport.refresh()
        except Exception as e:
            result['message'] = f'Runtime error: {e}\n{traceback.format_exc()}'
            self._try_undo(timeline, start_marker)
            return result
        t1 = time.time()

        result['success'] = True
        result['exec_time_ms'] = round((t1 - t0) * 1000)
        result['message'] = f'Model built in {result["exec_time_ms"]}ms'

        # ── Collect info about what was created ──
        if isinstance(build_result, dict):
            result['bodies'] = build_result.get('bodies', [])
            result['params'] = build_result.get('params', {})
            result['features'] = build_result.get('features', [])
            result['component'] = build_result.get('component', None)
        
        # Also collect user parameters
        result['params'] = self._collect_params()

        return result

    def execute_drawing(self, code: str, component, gdt_data: dict) -> dict:
        """
        Execute the create_drawing() function.
        Returns: {success, message, drawing_doc}
        """
        result = {'success': False, 'message': '', 'drawing_doc': None}

        is_safe, reason = self._validate(code)
        if not is_safe:
            result['message'] = f'Security: {reason}'
            return result

        try:
            compiled = compile(code, '<TextToCAD_drawing>', 'exec')
        except SyntaxError as e:
            result['message'] = f'Syntax error: {e}'
            return result

        namespace = self._make_namespace()
        try:
            exec(compiled, namespace)
        except Exception as e:
            result['message'] = f'Definition error: {e}'
            return result

        create_fn = namespace.get('create_drawing')
        if not callable(create_fn):
            result['message'] = 'No create_drawing() function found.'
            return result

        try:
            drawing_doc = create_fn(self.app, component, gdt_data or {})
            adsk.doEvents()
            result['success'] = True
            result['message'] = 'Drawing created.'
            result['drawing_doc'] = drawing_doc
        except Exception as e:
            result['message'] = f'Drawing error: {e}\n{traceback.format_exc()}'

        return result

    # ── Internal helpers ──

    def _validate(self, code: str) -> tuple:
        """Security validation."""
        code_lower = code.lower()
        for token in self.BLOCKED_TOKENS:
            if token.lower() in code_lower:
                return False, f'Blocked: {token}'
        if len(code) > 80_000:
            return False, 'Code too large (>80KB)'
        return True, ''

    def _make_namespace(self) -> dict:
        """Create a sandboxed execution namespace."""
        return {
            '__builtins__': {
                'range': range, 'len': len, 'int': int, 'float': float,
                'str': str, 'bool': bool, 'list': list, 'dict': dict,
                'tuple': tuple, 'set': set, 'enumerate': enumerate,
                'zip': zip, 'map': map, 'filter': filter, 'sorted': sorted,
                'min': min, 'max': max, 'abs': abs, 'round': round,
                'sum': sum, 'any': any, 'all': all, 'isinstance': isinstance,
                'type': type, 'hasattr': hasattr, 'getattr': getattr,
                'setattr': setattr, 'property': property, 'staticmethod': staticmethod,
                'classmethod': classmethod, 'super': super, 'object': object,
                'print': lambda *a, **kw: None,
                'True': True, 'False': False, 'None': None,
                '__import__': self._safe_import,
                'ValueError': ValueError, 'TypeError': TypeError,
                'RuntimeError': RuntimeError, 'KeyError': KeyError,
                'IndexError': IndexError, 'AttributeError': AttributeError,
                'Exception': Exception,
            }
        }

    def _safe_import(self, name, *args, **kwargs):
        """Only allow approved modules."""
        base = name.split('.')[0]
        if base in self.ALLOWED_MODULES or name in self.ALLOWED_MODULES:
            return __import__(name, *args, **kwargs)
        raise ImportError(f'Module "{name}" not allowed.')

    def _try_undo(self, timeline, start_marker: int):
        """Best-effort undo of operations after start_marker."""
        try:
            if timeline.count > start_marker:
                timeline.item(start_marker).rollTo(True)
        except:
            pass

    def _collect_params(self) -> dict:
        """Collect all user parameters and their values."""
        params = {}
        user_params = self.design.userParameters
        for i in range(user_params.count):
            p = user_params.item(i)
            params[p.name] = {
                'expression': p.expression,
                'value': p.value,
                'unit': p.unit,
                'comment': p.comment or ''
            }
        return params
