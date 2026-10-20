"""Run optional real-browser UI regressions against local response fixtures.

Usage: python tests/browser_smoke.py --output /tmp/evoagent-browser-results.json
Requires playwright and a local Chromium; never contacts model or GitHub services.
"""
import argparse
import json
from pathlib import Path
import shutil
import re
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
TASK_ID = '11111111-2222-4333-8444-555555555555'
REVIEWED_SHA = 'a' * 40


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='/tmp/evoagent-browser-results.json')
    args = parser.parse_args()
    fixture = {'detail_calls': 0, 'resumes': 0, 'reviews': 0, 'publication': 'pending', 'xss': False,
               'withheld': False, 'rollback': 0, 'evolution': 0, 'empty_dataset': False}
    results = []
    errors = []
    def check(name, condition):
        results.append({'test': name, 'passed': bool(condition)})
        if not condition:
            raise AssertionError(name)
    def task():
        return {'id': TASK_ID, 'repository': 'demo/repo', 'pull_request': 3, 'state': 'SUCCESS',
                'created_at': '2026-01-01T01:01:00Z', 'updated_at': '2026-01-01T01:02:00Z',
                'input': {'source': 'github-webhook', 'publish_review': True,
                          'github_issue_url': 'https://api.github.com/repos/demo/repo/issues/3'},
                'publication_status': fixture['publication'],
                'publication_error': ('Resource not accessible by personal access token <img src=x onerror="window.bad=1">'
                                      if fixture['publication'] == 'failed' else None),
                'report': {'summary': 'A candidate was withheld.' if fixture['withheld'] else 'One actionable finding.',
                           'risk': 'unknown' if fixture['withheld'] else 'high',
                           'reviewer': 'agentic-reviewer', 'run_mode': {'effective': 'agentic'},
                           'findings': [] if fixture['withheld'] else [{'rule_id': 'average-empty', 'severity': 'high',
                               'title': 'Division by zero', 'path': 'stats.py', 'line': 3,
                               'explanation': 'Empty input divides by zero.', 'evidence': 'return sum(values) / len(values)',
                               'fix': 'Add an empty guard.', 'test': 'Check average([]).', 'confidence': 1.0}],
                           'collaboration': {'protocol': 'lead-workers', 'worker_results': []},
                           'execution': {'llm_calls': 5, 'total_tokens': 1000, 'duration_ms': 100,
                                         'reviewed_revision': {'head_sha': REVIEWED_SHA},
                                         'review_status': 'needs_review' if fixture['withheld'] else 'complete',
                                         'finding_counts': {'raw_candidates': 2, 'lead_selected': 2,
                                             'gate_accepted': 0 if fixture['withheld'] else 2,
                                             'gate_rejected': 2 if fixture['withheld'] else 0,
                                             'final_published': 0 if fixture['withheld'] else 1},
                                         'failed_model_calls': 0, 'failed_tool_calls': 0,
                                         'rejected_findings': ([{'rule_id': 'withheld-rule', 'path': 'stats.py', 'line': 3,
                                           'reasons': ['Missing verified scope'], 'finding': {'title': 'Withheld candidate',
                                           'explanation': 'Needs verification', 'evidence': 'some code'}}] if fixture['withheld'] else [])}}}
    def api_reply(url, method):
        path = urlparse(url).path
        if path == '/api/dashboard':
            data = {'stats': {'tasks_total': 1, 'tasks_success': 1, 'tasks_failed': 0, 'success_rate': 1,
                             'unresolved_failure_cases': 0, 'active_skill_versions': 0},
                    'llm': {'enabled': True, 'provider': 'local-fixture', 'model': 'offline'},
                    'tasks': [task()], 'queue': 'memory-acked'}
        elif path == '/api/tasks':
            data = {'tasks': [task()]}
        elif path == '/v1/tasks/' + TASK_ID:
            fixture['detail_calls'] += 1
            data = task()
        elif path.endswith('/feedback'):
            data = {'feedback': []}
        elif path == '/v1/tasks/' + TASK_ID + '/resume':
            fixture['resumes'] += 1
            fixture['publication'] = 'sent'
            data = {'resumed': True, 'state': 'SUCCESS', 'publication_status': 'sent'}
        elif path == '/api/skills':
            data = {'skills': [{'name': 'security-review', 'kind': 'agent-skill', 'description': 'Security',
                               'version': '1', 'resources': [], 'permissions': []}],
                    'llm': {'enabled': True, 'provider': 'local-fixture', 'model': 'offline'}}
        elif path == '/v1/evolution/status':
            data = {'ready': True, 'validation_cases': 5, 'holdout_cases': 2, 'minimum_cases': 3,
                    'minimum_holdout_cases': 2, 'model_configured': True, 'minimum_improvement': .01,
                    'validation_total_cases': 8, 'validation_selected_cases': 5, 'validation_omitted_cases': 3,
                    'holdout_total_cases': 2, 'holdout_selected_cases': 2, 'holdout_omitted_cases': 0,
                    'production_activation_allowed': True}
            if fixture['empty_dataset']:
                data.update(ready=False, validation_cases=0, holdout_cases=0,
                            validation_total_cases=0, validation_selected_cases=0, validation_omitted_cases=0,
                            holdout_total_cases=0, holdout_selected_cases=0, holdout_omitted_cases=0)
        elif path == '/v1/evolution/runs':
            data = {'runs': []}
        elif path == '/api/failures':
            data = {'cases': []}
        elif path == '/v1/evolution/rollback':
            fixture['rollback'] += 1
            data = {'rolled_back': True}
        elif path == '/v1/evolution/propose':
            fixture['evolution'] += 1
            data = {'decision': 'activated', 'reason': 'Shared runtime passed',
                    'version': {'skill_name': 'llm-review', 'version': 2, 'active': True}}
        elif path == '/v1/reviews':
            fixture['reviews'] += 1
            data = {'error': 'Not expected in this UI fixture'}
        elif path.startswith(('/api/', '/v1/')):
            return {'status': 404, 'body': {'error': path}}
        else:
            return {'status': 404, 'body': {'error': path}}
        return {'status': 200, 'body': data}
    try:
        with sync_playwright() as playwright:
            options = {'headless': True, 'args': ['--no-sandbox']}
            if shutil.which('chromium'):
                options['executable_path'] = shutil.which('chromium')
            browser = playwright.chromium.launch(**options)
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.on('pageerror', lambda error: errors.append(str(error)))
            # No browser network permissions are needed: load the real HTML/CSS/JS,
            # replacing only fetch with an asynchronous API fixture.
            page.expose_function('_evoTestReply', api_reply)
            page.clock.install()
            html = (ROOT/'web/index.html').read_text()
            html = re.sub(r'<script[^>]*src=[^>]*>\s*</script>', '', html)
            html = re.sub(r'<link[^>]*rel="stylesheet"[^>]*>', '', html)
            page.set_content(html)
            page.add_style_tag(content=(ROOT/'web/app.css').read_text() + '\n' + (ROOT/'web/login.css').read_text())
            page.evaluate("""() => { window.fetch = async (url, options={}) => {
                const reply = await window._evoTestReply(String(url), options.method || 'GET');
                return new Response(JSON.stringify(reply.body), {status: reply.status,
                    headers: {'Content-Type': 'application/json'}});
            }; }""")
            page.add_script_tag(content=(ROOT/'web/app.js').read_text())
            page.locator('#recent-tasks [data-task]').click()
            page.locator('[data-publication-status="pending"]').wait_for()
            check('review success still displays independent pending publication', True)
            check('full reviewed SHA is visible', REVIEWED_SHA in page.locator('#task-overview').inner_text())
            check('raw, selected, qualified and published counts are separate',
                  all(text in page.locator('#report-findings').inner_text() for text in
                      ('原始候选 2', 'Lead 接纳 2', '门禁通过 2', '最终结论 1')))
            before = fixture['detail_calls']
            fixture['publication'] = 'failed'
            page.clock.run_for(12500)
            page.locator('[data-publication-status="failed"]').wait_for(timeout=5000)
            check('SUCCESS plus pending continues polling task details', fixture['detail_calls'] > before)
            check('403 permission message is visible', 'Pull requests' in page.locator('.publication-panel').inner_text())
            check('publication error HTML is escaped', page.locator('.publication-panel img').count() == 0 and page.evaluate('window.bad') is None)
            page.locator('[data-retry-publication]').click()
            page.locator('#confirm-cancel').click()
            check('cancel retry does not issue a write', fixture['resumes'] == 0)
            page.locator('[data-retry-publication]').click()
            page.locator('#confirm-ok').click()
            page.locator('[data-publication-status="sent"]').wait_for()
            check('comment-only resume does not POST new review', fixture['resumes'] == 1 and fixture['reviews'] == 0)
            check('sent state removes retry button', page.locator('[data-retry-publication]').count() == 0)
            check('safe PR link points to Conversation page', page.locator('.publication-panel a').get_attribute('href') == 'https://github.com/demo/repo/pull/3')
            page.locator('#tab-json').click()
            check('raw JSON reflects updated sent status', '"publication_status": "sent"' in page.locator('#task-report').inner_text())
            fixture['withheld'] = True
            page.locator('#refresh').click()
            page.locator('#tab-findings').click()
            page.locator('#report-findings summary').first.click()
            check('withheld candidates and reasons remain visible', 'Missing verified scope' in page.locator('#report-findings').inner_text())
            check('withheld report displays unknown risk and review status',
                  '风险等级 待复核' in page.locator('#task-overview').inner_text()
                  and '仍需人工复核' in page.locator('#task-overview').inner_text())
            fixture['publication'] = 'superseded'
            page.locator('#refresh').click()
            page.locator('[data-publication-status="superseded"]').wait_for()
            check('superseded publication has no retry action', page.locator('[data-retry-publication]').count() == 0)
            for view in ('overview', 'review', 'skills', 'evolution', 'tasks'):
                page.locator('.nav-item[data-view="%s"]' % view).click()
                page.locator('#view-' + view + '.active').wait_for()
                check('existing view retained: ' + view, True)
            page.locator('.nav-item[data-view="evolution"]').click()
            check('evaluation selection totals are visible', '总计 8，本轮 5，未选 3' in
                  page.locator('#evolution-metrics').inner_text())
            page.locator('#evolution-form button[type="submit"]').click()
            page.locator('#confirm-ok').click()
            page.wait_for_function('document.querySelector("#evolution-result").textContent.includes("已激活")')
            check('Prompt proposal still connected to real API contract', fixture['evolution'] == 1)
            page.locator('#rollback-prompt').click()
            page.locator('#confirm-ok').click()
            page.wait_for_function('document.querySelector("#toast").textContent.includes("基线")')
            check('Prompt rollback button uses rollback API', fixture['rollback'] == 1)
            fixture['empty_dataset'] = True
            page.locator('.nav-item[data-view="skills"]').click()
            page.locator('.nav-item[data-view="evolution"]').click()
            page.wait_for_function('document.querySelector("#evolution-ready").textContent === "评测集不足"')
            check('empty database explicitly reports insufficient evaluation cases', True)
            page.locator('#theme-toggle').click()
            check('theme switching retained', page.locator('html').get_attribute('data-theme') == 'dark')
            page.set_viewport_size({'width': 390, 'height': 844})
            page.wait_for_timeout(250)
            check('mobile layout has no horizontal overflow', page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'))
            check('no browser JavaScript errors', not errors)
            browser.close()
    finally:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps({'tests': results, 'errors': errors, 'fixture_calls': fixture},
                                                ensure_ascii=False, indent=2))
    print(json.dumps({'passed': len(results), 'javascript_errors': errors}, ensure_ascii=False))


if __name__ == '__main__':
    main()
