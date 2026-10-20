"""Atomic Skill transitions shared by SQLite and PostgreSQL adapters."""
from contextlib import nullcontext
import hashlib
import json


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


class AtomicSkillStore:
    def _skill_transaction(self, conn, tenant_id, skill_name):
        if hasattr(self, 'psycopg'):
            conn.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (tenant_id + ':' + skill_name,))
        else:
            conn.execute('BEGIN IMMEDIATE')

    def _skill_execute(self, conn, sql, params=()):
        return conn.execute(sql.replace('?', '%s') if hasattr(self, 'psycopg') else sql, params)

    @staticmethod
    def _skill_json(value):
        return json.loads(value) if isinstance(value, str) else value

    def commit_skill_activation(self, skill_name, version, run_id, tenant_id,
                                expected_active_version=None):
        with getattr(self, '_lock', nullcontext()), self._connect() as conn:
            self._skill_transaction(conn, tenant_id, skill_name)
            run = self._skill_execute(conn, 'SELECT * FROM skill_evolution_runs WHERE id=? AND tenant_id=? '
                                     'AND skill_name=? AND candidate_version=?',
                                     (run_id, tenant_id, skill_name, version)).fetchone()
            if not run or run['decision'] not in {'ready', 'activated'}:
                return False
            active = self._skill_execute(conn, 'SELECT version FROM skill_artifact_versions WHERE tenant_id=? '
                                        'AND skill_name=? AND active=?', (tenant_id, skill_name, True)).fetchone()
            if (int(active['version']) if active else None) != expected_active_version:
                return False
            candidate = self._skill_execute(conn, 'SELECT * FROM skill_artifact_versions WHERE tenant_id=? '
                                           'AND skill_name=? AND version=?', (tenant_id, skill_name, version)).fetchone()
            provenance = self._skill_json(run['metrics_json']).get('reproducibility') or {}
            if not candidate or candidate['artifact_sha256'] != provenance.get('candidate_artifact_sha256'):
                return False
            self._skill_execute(conn, 'UPDATE skill_artifact_versions SET active=? WHERE tenant_id=? AND skill_name=?',
                                (False, tenant_id, skill_name))
            self._skill_execute(conn, 'UPDATE skill_artifact_versions SET active=? WHERE tenant_id=? '
                                'AND skill_name=? AND version=?', (True, tenant_id, skill_name, version))
            self._skill_execute(conn, "UPDATE skill_evolution_runs SET decision='activated' WHERE id=?", (run_id,))
            return True

    def restore_skill_baseline(self, skill_name, activated_version, run_id, baseline_version, tenant_id):
        with getattr(self, '_lock', nullcontext()), self._connect() as conn:
            self._skill_transaction(conn, tenant_id, skill_name)
            run = self._skill_execute(conn, 'SELECT * FROM skill_evolution_runs WHERE id=? AND tenant_id=? '
                                     'AND skill_name=? AND candidate_version=?',
                                     (run_id, tenant_id, skill_name, activated_version)).fetchone()
            active = self._skill_execute(conn, 'SELECT version FROM skill_artifact_versions WHERE tenant_id=? '
                                        'AND skill_name=? AND active=?', (tenant_id, skill_name, True)).fetchone()
            if not run or run['decision'] != 'activated' or not active or int(active['version']) != activated_version:
                return False
            if run['baseline_version'] != baseline_version:
                return False
            provenance = self._skill_json(run['metrics_json']).get('reproducibility') or {}
            if baseline_version is not None:
                baseline = self._skill_execute(conn, 'SELECT artifact_json FROM skill_artifact_versions WHERE tenant_id=? '
                                              'AND skill_name=? AND version=?',
                                              (tenant_id, skill_name, baseline_version)).fetchone()
                if not baseline or _digest(self._skill_json(baseline['artifact_json'])) != provenance.get('baseline_artifact_sha256'):
                    return False
            elif provenance.get('baseline_artifact') is not None:
                return False
            self._skill_execute(conn, 'UPDATE skill_artifact_versions SET active=? WHERE tenant_id=? AND skill_name=?',
                                (False, tenant_id, skill_name))
            if baseline_version is not None:
                self._skill_execute(conn, 'UPDATE skill_artifact_versions SET active=? WHERE tenant_id=? '
                                    'AND skill_name=? AND version=?', (True, tenant_id, skill_name, baseline_version))
            self._skill_execute(conn, "UPDATE skill_evolution_runs SET decision='rolled_back' WHERE id=?", (run_id,))
            return True

    def update_skill_evolution_run(self, run_id, decision, metrics, tenant_id):
        with getattr(self, '_lock', nullcontext()), self._connect() as conn:
            cast = '::jsonb' if hasattr(self, 'psycopg') else ''
            cursor = self._skill_execute(conn, 'UPDATE skill_evolution_runs SET decision=?,metrics_json=?' + cast +
                                        ' WHERE id=? AND tenant_id=?',
                                        (decision, json.dumps(metrics, ensure_ascii=False), run_id, tenant_id))
            return cursor.rowcount > 0

    def get_prompt_version(self, skill_name, version):
        with self._connect() as conn:
            row = self._skill_execute(conn, "SELECT * FROM skill_versions WHERE skill_name=? AND version=?",
                                      (skill_name, version)).fetchone()
            return dict(row) if row else None

    def get_prompt_run(self, skill_name, version):
        with self._connect() as conn:
            row = self._skill_execute(conn, "SELECT * FROM evolution_runs WHERE skill_name=? AND candidate_version=? "
                                      "ORDER BY created_at DESC LIMIT 1", (skill_name, version)).fetchone()
            if not row:
                return None
            result = dict(row)
            result["metrics"] = self._skill_json(result.pop("metrics_json"))
            return result

    def commit_prompt_activation(self, skill_name, version, run_id, expected_active_version):
        with getattr(self, '_lock', nullcontext()), self._connect() as conn:
            self._skill_transaction(conn, "prompt-global", skill_name)
            run = self._skill_execute(conn, "SELECT * FROM evolution_runs WHERE id=? AND skill_name=? AND candidate_version=?",
                                      (run_id, skill_name, version)).fetchone()
            active = self._skill_execute(conn, "SELECT version FROM skill_versions WHERE skill_name=? AND active=?",
                                         (skill_name, True)).fetchone()
            candidate = self._skill_execute(conn, "SELECT prompt FROM skill_versions WHERE skill_name=? AND version=?",
                                            (skill_name, version)).fetchone()
            if (not run or run['decision'] != 'ready' or not candidate
                    or (int(active['version']) if active else None) != expected_active_version):
                return False
            metadata = self._skill_json(run['metrics_json'])
            production = metadata.get('production', {})
            if hashlib.sha256(json.dumps(production.get('candidate_config', {}), sort_keys=True).encode()).hexdigest() != production.get('candidate_config_sha256'):
                return False
            proof = metadata.get('reproducibility', {})
            if hashlib.sha256(candidate['prompt'].encode()).hexdigest() != proof.get('candidate_prompt_sha256'):
                return False
            self._skill_execute(conn, "UPDATE skill_versions SET active=? WHERE skill_name=?", (False, skill_name))
            self._skill_execute(conn, "UPDATE skill_versions SET active=? WHERE skill_name=? AND version=?", (True, skill_name, version))
            self._skill_execute(conn, "UPDATE evolution_runs SET decision='activated' WHERE id=?", (run_id,))
            return True

    def restore_prompt_baseline(self, skill_name, version, run_id, baseline_version):
        with getattr(self, '_lock', nullcontext()), self._connect() as conn:
            self._skill_transaction(conn, "prompt-global", skill_name)
            run = self._skill_execute(conn, "SELECT * FROM evolution_runs WHERE id=? AND skill_name=? AND candidate_version=?",
                                      (run_id, skill_name, version)).fetchone()
            active = self._skill_execute(conn, "SELECT version FROM skill_versions WHERE skill_name=? AND active=?",
                                         (skill_name, True)).fetchone()
            if (not run or run['decision'] != 'activated' or not active or int(active['version']) != version
                    or run['baseline_version'] != baseline_version):
                return False
            proof = self._skill_json(run['metrics_json']).get('production', {})
            if baseline_version is not None:
                baseline = self._skill_execute(conn, "SELECT prompt FROM skill_versions WHERE skill_name=? AND version=?",
                                               (skill_name, baseline_version)).fetchone()
                if not baseline or baseline['prompt'] != proof.get('baseline_prompt'):
                    return False
            elif proof.get('baseline_prompt') != '':
                return False
            self._skill_execute(conn, "UPDATE skill_versions SET active=? WHERE skill_name=?", (False, skill_name))
            if baseline_version is not None:
                self._skill_execute(conn, "UPDATE skill_versions SET active=? WHERE skill_name=? AND version=?", (True, skill_name, baseline_version))
            self._skill_execute(conn, "UPDATE evolution_runs SET decision='rolled_back' WHERE id=?", (run_id,))
            return True
