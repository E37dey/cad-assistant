"""
ai_engine.py — Enhanced AI engine for Claude Code integration.
Supports multi-stage generation: 3D model → GD&T → Drawing.
"""

import json
import re

try:
    import urllib.request
    import urllib.error
    HAS_URLLIB = True
except ImportError:
    HAS_URLLIB = False

import os
import sys
PROMPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), 'prompts')
if PROMPTS_DIR not in sys.path:
    sys.path.insert(0, PROMPTS_DIR)

from prompts import (
    MODELING_SYSTEM_PROMPT,
    GDT_SYSTEM_PROMPT,
    DRAWING_SYSTEM_PROMPT,
    COMBINED_SYSTEM_PROMPT,
)

ANTHROPIC_API_URL = 'https://api.anthropic.com/v1/messages'
MODEL = 'claude-sonnet-4-6'


class AIEngine:
    """Multi-stage AI engine for model generation, GD&T, and drawings."""

    def __init__(self, api_key: str):
        self.api_key = api_key

    # ── API Call ──

    def _call_api(self, system: str, user_msg: str, max_tokens: int = 8192) -> str:
        """Send a request to the Anthropic API and return the text response."""
        if not HAS_URLLIB:
            raise RuntimeError('urllib not available')

        payload = json.dumps({
            'model': MODEL,
            'max_tokens': max_tokens,
            'system': system,
            'messages': [{'role': 'user', 'content': user_msg}]
        }).encode('utf-8')

        headers = {
            'Content-Type': 'application/json',
            'x-api-key': self.api_key,
            'anthropic-version': '2023-06-01'
        }

        req = urllib.request.Request(
            ANTHROPIC_API_URL, data=payload, headers=headers, method='POST'
        )

        try:
            with urllib.request.urlopen(req, timeout=180) as resp:
                body = json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            err = e.read().decode('utf-8', errors='replace')
            raise RuntimeError(f'API HTTP {e.code}: {err}')

        text_parts = [
            b['text'] for b in body.get('content', []) if b.get('type') == 'text'
        ]
        return '\n'.join(text_parts)

    # ── Extraction helpers ──

    @staticmethod
    def _extract_python(text: str, marker: str = '') -> object:
        """Extract a Python code block, optionally matching a marker comment."""
        # Find all python code blocks
        pattern = r'```python\s*\n(.*?)```'
        blocks = re.findall(pattern, text, re.DOTALL)

        if marker:
            for block in blocks:
                if marker in block:
                    return block.strip()

        # Fallback: return first block with 'def build(' or 'def create_drawing('
        for block in blocks:
            if 'def build(' in block or 'def create_drawing(' in block:
                return block.strip()

        return blocks[0].strip() if blocks else None

    @staticmethod
    def _extract_json(text: str) -> object:
        """Extract a JSON block from the response."""
        pattern = r'```json\s*\n(.*?)```'
        match = re.search(pattern, text, re.DOTALL)
        if match:
            raw = match.group(1).strip()
            # Remove comments (// style) that Claude sometimes adds
            lines = [
                l for l in raw.split('\n')
                if not l.strip().startswith('//')
            ]
            cleaned = '\n'.join(lines)
            try:
                return json.loads(cleaned)
            except json.JSONDecodeError:
                pass
        return None

    # ── Stage 1: Generate 3D Model ──

    def generate_model(self, prompt: str, detail: str = 'standard',
                       units: str = 'mm', as_component: bool = True) -> object:
        """Generate the build() function for 3D modeling."""
        user_msg = f"""Create a Fusion 360 parametric model:

DESCRIPTION: {prompt}

SETTINGS:
- Detail level: {detail}
- Units: {units}
- Create as component: {as_component}
- Include user parameters for ALL dimensions
- Use isComputeDeferred for performance
- Name all bodies and features

Return ONLY the `def build(rootComp, config):` function."""

        response = self._call_api(MODELING_SYSTEM_PROMPT, user_msg)
        code = self._extract_python(response, 'def build(')
        if code and 'def build(' in code:
            return code
        return None

    # ── Stage 2: Generate GD&T Specification ──

    def generate_gdt(self, prompt: str, model_info: dict = None) -> object:
        """Generate GD&T specification for the part."""
        model_ctx = ''
        if model_info:
            model_ctx = f"""
MODEL INFORMATION:
- Bodies: {model_info.get('bodies', [])}
- Parameters: {json.dumps(model_info.get('params', {}), indent=2)}
- Features: {model_info.get('features', [])}
"""

        user_msg = f"""Determine proper GD&T callouts for this part:

DESCRIPTION: {prompt}
{model_ctx}
Generate a complete GD&T specification including:
1. Datum feature selection with justification
2. Feature control frames for all critical features
3. Surface finish requirements
4. Dimensional tolerances with fit classes where applicable
5. General tolerance standard

Return ONLY the JSON specification."""

        response = self._call_api(GDT_SYSTEM_PROMPT, user_msg)
        return self._extract_json(response)

    # ── Stage 3: Generate Drawing ──

    def generate_drawing(self, prompt: str, gdt_data: dict = None) -> object:
        """Generate the create_drawing() function for 2D drawings."""
        gdt_ctx = ''
        if gdt_data:
            gdt_ctx = f"""
GD&T SPECIFICATION:
{json.dumps(gdt_data, indent=2)}
"""

        user_msg = f"""Create a complete mechanical drawing for this part:

DESCRIPTION: {prompt}
{gdt_ctx}
The drawing should include:
1. Standard views (front, top, right side, isometric)
2. Section views for internal features (bores, pockets, etc.)
3. Detail views for small features (threads, chamfers)
4. All dimensions with appropriate tolerances
5. GD&T symbols (datums, feature control frames)
6. Surface finish symbols
7. Title block with part info
8. General notes

Return ONLY the `def create_drawing(app, component, gdt_data):` function."""

        response = self._call_api(DRAWING_SYSTEM_PROMPT, user_msg, max_tokens=8192)
        code = self._extract_python(response, 'def create_drawing(')
        if code and 'def create_drawing(' in code:
            return code
        return None

    # ── Combined: All three stages in one call ──

    def generate_all(self, prompt: str, detail: str = 'detailed',
                     units: str = 'mm') -> dict:
        """
        Generate model + GD&T + drawing in one API call.
        Returns dict with keys: model_code, gdt_spec, drawing_code
        """
        user_msg = f"""Create a complete engineering package for:

DESCRIPTION: {prompt}

SETTINGS:
- Detail level: {detail} (include fillets, chamfers, threads as appropriate)
- Units: {units}
- Create as new component
- Include full GD&T per ASME Y14.5-2018
- Generate complete mechanical drawing

Produce all three outputs as specified."""

        response = self._call_api(COMBINED_SYSTEM_PROMPT, user_msg, max_tokens=16000)

        result = {
            'model_code': None,
            'gdt_spec': None,
            'drawing_code': None,
            'raw_response': response,
        }

        # Extract all python blocks
        py_blocks = re.findall(r'```python\s*\n(.*?)```', response, re.DOTALL)
        for block in py_blocks:
            if 'def build(' in block:
                result['model_code'] = block.strip()
            elif 'def create_drawing(' in block:
                result['drawing_code'] = block.strip()

        # Extract JSON block
        result['gdt_spec'] = self._extract_json(response)

        return result

    # ── Retry with error context ──

    def retry_model(self, prompt: str, error: str, prev_code: str,
                    **kwargs) -> object:
        """Regenerate model code with error context."""
        retry_prompt = f"""{prompt}

⚠️ PREVIOUS ATTEMPT FAILED WITH ERROR:
{error}

BROKEN CODE:
```python
{prev_code[:3000]}
```

Fix the issue. Common causes:
- Wrong profile index (use sketch.profiles to check)
- Missing sketch.isComputeDeferred = False before accessing profiles
- Dimension in wrong units (must be cm internally)
- Feature operation on wrong body
"""
        return self.generate_model(retry_prompt, **kwargs)
