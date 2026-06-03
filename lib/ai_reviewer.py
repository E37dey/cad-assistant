"""
ai_reviewer.py — AI-powered engineering review.

While engineering_validator.py does geometric/programmatic checks on the
actual Fusion 360 model, this module uses Claude to do *intelligent* review:

- Does the design make engineering SENSE?
- Are tolerances appropriate for the application?
- Is the GD&T complete and correct per ASME Y14.5?
- Is the drawing sufficient for manufacturing?
- Are there design improvements the engineer should consider?
"""

import json
import os, sys

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), 'prompts')
if PROMPTS_DIR not in sys.path:
    sys.path.insert(0, PROMPTS_DIR)


AI_REVIEW_SYSTEM_PROMPT = r"""You are a senior mechanical engineer performing a design review.
You are reviewing a part that was generated from a text description.

You must check THREE things and produce a structured JSON report:

## 1. STRUCTURAL REVIEW
- Does the geometry make sense for the described function?
- Are wall thicknesses adequate for the loads?
- Are stress concentrators addressed (fillets at transitions)?
- Is the part stiff enough, or will it deflect excessively?
- Are fastener sizes appropriate for the loads?
- Is the material choice reasonable?

## 2. GD&T REVIEW (per ASME Y14.5-2018)
- Are datums selected correctly? (Primary = mounting surface)
- Are feature control frames complete and correct?
- Are tolerance values appropriate? (not too tight = expensive, not too loose = won't work)
- Is MMC/LMC applied correctly? (only on features of size)
- Are form tolerances on datum features?
- Is the general tolerance standard specified?
- Are surface finishes appropriate for function?

Rules you must enforce:
- Form tolerances (flatness, straightness, circularity, cylindricity) CANNOT have MMC/LMC
- Position tolerance MUST reference at least one datum
- Concentricity should be replaced with position + RFS unless mass balance is critical
- Profile can often replace multiple form/orientation callouts
- Tighter tolerance = higher cost; only tighten what's functionally necessary

## 3. DRAWING REVIEW
- Are all critical dimensions shown?
- Are views sufficient? (enough views to fully define the part)
- Is a section view needed for internal features?
- Are detail views needed for small features?
- Is the title block complete?
- Are general notes present? (tolerance, surface finish, deburr, material)
- Are all holes dimensioned with both size AND position?
- Is there a clear datum reference frame?

## OUTPUT FORMAT
Return ONLY this JSON (no markdown, no explanation):
{
  "overall_grade": "A/B/C/D/F",
  "structural": {
    "grade": "A-F",
    "issues": [
      {
        "severity": "error|warning|info",
        "code": "SHORT_CODE",
        "title": "Brief title",
        "detail": "Full explanation",
        "fix": "How to fix it"
      }
    ]
  },
  "gdt": {
    "grade": "A-F",
    "issues": [...]
  },
  "drawing": {
    "grade": "A-F",
    "issues": [...]
  },
  "improvements": [
    "Suggested design improvement 1",
    "Suggested design improvement 2"
  ]
}

Grade scale:
- A: Production-ready, no issues
- B: Minor improvements suggested, functional
- C: Some issues that should be fixed
- D: Significant problems, needs rework
- F: Critical issues, part will fail or can't be manufactured
"""


class AIReviewer:
    """Uses Claude to perform intelligent engineering review."""

    def __init__(self, ai_engine):
        """
        Args:
            ai_engine: An AIEngine instance (from ai_engine.py)
        """
        self.ai = ai_engine

    def review(self, prompt: str, model_params: dict,
               gdt_spec: dict = None, process: str = 'CNC Machining',
               material: str = 'Steel') -> dict:
        """
        Perform a full AI-powered engineering review.

        Args:
            prompt: Original part description
            model_params: Dict of user parameters {name: {expression, value, unit}}
            gdt_spec: GD&T specification (from gdt_manager)
            process: Manufacturing process
            material: Material

        Returns:
            Review report as dict
        """
        user_msg = f"""Review this engineering design:

ORIGINAL DESCRIPTION:
{prompt}

MATERIAL: {material}
MANUFACTURING PROCESS: {process}

MODEL PARAMETERS:
{json.dumps(model_params, indent=2)}

GD&T SPECIFICATION:
{json.dumps(gdt_spec, indent=2) if gdt_spec else 'Not provided'}

Perform a complete structural, GD&T, and drawing review.
Return ONLY the JSON report."""

        response = self.ai._call_api(
            AI_REVIEW_SYSTEM_PROMPT, user_msg, max_tokens=4096
        )

        # Parse JSON from response
        try:
            # Try direct parse first
            return json.loads(response.strip())
        except json.JSONDecodeError:
            pass

        # Try extracting from code block
        import re
        match = re.search(r'```(?:json)?\s*\n(.*?)```', response, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except:
                pass

        # Try finding JSON object in text
        match = re.search(r'\{.*\}', response, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except:
                pass

        return {
            'overall_grade': '?',
            'error': 'Failed to parse AI review response',
            'raw_response': response[:2000],
        }

    def format_report(self, report: dict) -> str:
        """Format the AI review report as readable text."""
        lines = [
            '═' * 50,
            '  AI ENGINEERING REVIEW',
            '═' * 50,
            '',
            f'  Overall grade: {report.get("overall_grade", "?")}',
            '',
        ]

        grade_emoji = {
            'A': '🟢', 'B': '🔵', 'C': '🟡', 'D': '🟠', 'F': '🔴'
        }
        severity_emoji = {'error': '❌', 'warning': '⚠️', 'info': 'ℹ️'}

        for domain in ['structural', 'gdt', 'drawing']:
            section = report.get(domain, {})
            grade = section.get('grade', '?')
            emoji = grade_emoji.get(grade, '⚪')

            lines.append(f'{emoji} {domain.upper()}: Grade {grade}')
            lines.append('─' * 40)

            for issue in section.get('issues', []):
                sev = severity_emoji.get(issue.get('severity', ''), '?')
                lines.append(f'  {sev} [{issue.get("code", "")}] {issue.get("title", "")}')
                lines.append(f'     {issue.get("detail", "")}')
                lines.append(f'     💡 {issue.get("fix", "")}')
                lines.append('')

        # Improvements
        improvements = report.get('improvements', [])
        if improvements:
            lines.append('💡 DESIGN IMPROVEMENTS:')
            lines.append('─' * 40)
            for imp in improvements:
                lines.append(f'  → {imp}')
            lines.append('')

        lines.append('═' * 50)
        return '\n'.join(lines)


def get_combined_report(programmatic_report: dict,
                         ai_report: dict) -> dict:
    """
    Merge the programmatic validation (engineering_validator.py)
    with the AI review into a single comprehensive report.
    """
    # Deduplicate issues by code
    seen_codes = set()
    all_issues = []

    # Add programmatic issues first (these are based on actual geometry)
    for issue in programmatic_report.get('issues', []):
        code = issue.get('code', '')
        if code not in seen_codes:
            seen_codes.add(code)
            issue['source'] = 'geometry_check'
            all_issues.append(issue)

    # Add AI issues that aren't duplicates
    for domain in ['structural', 'gdt', 'drawing']:
        for issue in ai_report.get(domain, {}).get('issues', []):
            code = issue.get('code', '')
            if code not in seen_codes:
                seen_codes.add(code)
                issue['source'] = 'ai_review'
                issue['domain'] = domain
                all_issues.append(issue)

    errors = sum(1 for i in all_issues if i.get('severity') == 'error')
    warnings = sum(1 for i in all_issues if i.get('severity') == 'warning')

    return {
        'overall_grade': ai_report.get('overall_grade', '?'),
        'summary': {
            'total': len(all_issues),
            'errors': errors,
            'warnings': warnings,
            'pass': errors == 0,
        },
        'issues': all_issues,
        'improvements': ai_report.get('improvements', []),
    }
