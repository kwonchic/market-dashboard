#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""scripts/update_toss_market_dashboard.sh 회귀 테스트 (로컬 임시 git 저장소만 사용, 네트워크·자격증명 없음).

- 운영 작업본이 원격과 갈라져 있어도(과거 pull --ff-only 실패 상태) 수집·배포가 계속되는지
- push 실패 시 별도 종료코드·상태 기록·아티팩트 보존, 운영 작업본 무변경
- 수집 실패 시 이전 수급 값 보존 + 실패 표시
- 동시 실행 잠금, GitHub writer 경쟁(push 거절 후 재구성 재시도)
- force push 미사용
"""
import json, os, shutil, subprocess, sys, tempfile, time, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "scripts", "update_toss_market_dashboard.sh")
SKIP_PARTS = {".git", "__pycache__", "logs"}


def sh(args, cwd=None, check=True, env=None):
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        raise AssertionError(f"{args} rc={r.returncode}\n{r.stdout}\n{r.stderr}")
    return r


def git(cwd, *a, check=True):
    return sh(["git", "-c", "user.name=t", "-c", "user.email=t@t", *a], cwd=cwd, check=check)


def records_json(end_iso="2026-11-16"):
    # 평일 22개(2026-11-16 월요일부터 과거로), 외국인 매일 +0.1조, 기관 -0.05조
    from datetime import date, timedelta
    d, out = date.fromisoformat(end_iso), []
    while len(out) < 22:
        if d.weekday() < 5:
            out.append({"date": d.isoformat(),
                        "foreigner": {"buyAmount": "5100000000000", "sellAmount": "5000000000000"},
                        "institution": {"buyAmount": "4950000000000", "sellAmount": "5000000000000"}})
        d -= timedelta(days=1)
    return out


FAKE_OK = r'''
import json, sys
sys.path.insert(0, sys.argv[2])
from datetime import datetime, timezone, timedelta
import kr_flows as kf
recs = json.load(open(sys.argv[3]))
now = datetime(2026, 11, 17, 8, 40, tzinfo=timezone(timedelta(hours=9)))
f = kf.compute_flows_from_records(recs, now)
art = {"schema": 1, "collected_at": f["observed_at"], "fields": {"kr.flows": f}, "failed": {},
       "raw": {"kr.flows": {"records": recs}}}
json.dump(art, open(sys.argv[1], "w"))
'''

FAKE_FAIL = r'''
import json, sys
json.dump({"schema": 1, "collected_at": "2026-11-17T08:40:00+09:00", "fields": {},
           "failed": {"kr.client": "client_unavailable", "kr.flows": "client_unavailable"}, "raw": {}},
          open(sys.argv[1], "w"))
sys.exit(11)
'''


@unittest.skipUnless(shutil.which("git") and shutil.which("bash"), "git/bash required")
class UpdateScript(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mdash-test-")
        self.remote = os.path.join(self.tmp, "remote.git")
        self.seed = os.path.join(self.tmp, "seed")
        self.live = os.path.join(self.tmp, "live")
        self.other = os.path.join(self.tmp, "github-writer")
        self.state = os.path.join(self.tmp, "state")
        sh(["git", "init", "-q", "--bare", "-b", "main", self.remote])
        shutil.copytree(ROOT, self.seed, ignore=lambda d, names: [n for n in names if n in SKIP_PARTS or n.endswith(".pyc")])
        git(self.seed, "init", "-q", "-b", "main")
        git(self.seed, "add", "-A")
        git(self.seed, "commit", "-q", "-m", "seed")
        git(self.seed, "remote", "add", "origin", self.remote)
        git(self.seed, "push", "-q", "origin", "main")
        git(self.tmp, "clone", "-q", self.remote, self.live)
        git(self.tmp, "clone", "-q", self.remote, self.other)
        self.recs = os.path.join(self.tmp, "records.json")
        json.dump(records_json(), open(self.recs, "w"))
        for name, body in (("fake_ok.py", FAKE_OK), ("fake_fail.py", FAKE_FAIL)):
            open(os.path.join(self.tmp, name), "w").write(body)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # helpers
    def env(self, collect_cmd):
        e = dict(os.environ)
        for k in list(e):
            if k.startswith("TOSS_") or k.startswith("MDASH_"):
                e.pop(k)
        e.update({"MDASH_REPO": self.live, "MDASH_ENV_FILE": os.path.join(self.tmp, "no-such.env"),
                  "MDASH_LOG_DIR": os.path.join(self.tmp, "logs"), "MDASH_STATE_DIR": self.state,
                  "MDASH_PYTHON": sys.executable, "MDASH_COLLECT_CMD": collect_cmd, "MDASH_RETRY_SLEEP": "0",
                  "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                  "GIT_COMMITTER_EMAIL": "t@t"})
        return e

    def ok_cmd(self):
        return f'"{sys.executable}" "{self.tmp}/fake_ok.py" "$1" "{self.live}" "{self.recs}"'

    def run_script(self, collect_cmd):
        return subprocess.run(["bash", SCRIPT, "_ignored"], capture_output=True, text=True, env=self.env(collect_cmd)).returncode

    def remote_data(self):
        out = sh(["git", "--git-dir", self.remote, "show", "main:data.json"]).stdout
        return json.loads(out)

    def remote_head(self):
        return sh(["git", "--git-dir", self.remote, "rev-parse", "main"]).stdout.strip()

    def status(self):
        return json.load(open(os.path.join(self.state, "status.json")))

    def diverge_live(self):
        # 과거 사고 재현: 운영 작업본에 미푸시 커밋 + 원격은 앞서감 → pull --ff-only 불가 상태
        p = os.path.join(self.live, "data.json")
        d = json.load(open(p)); d["kr"]["fx"]["num"] = 1360
        json.dump(d, open(p, "w"), ensure_ascii=False, indent=2)
        git(self.live, "commit", "-q", "-am", "local unpushed")
        self.github_writer_commit()

    def github_writer_commit(self, vix=99.9):
        git(self.other, "pull", "-q", "--ff-only", "origin", "main")
        p = os.path.join(self.other, "data.json")
        d = json.load(open(p)); d["us"]["vix"]["num"] = vix
        json.dump(d, open(p, "w"), ensure_ascii=False, indent=2)
        git(self.other, "commit", "-q", "-am", "auto update (github writer)")
        git(self.other, "push", "-q", "origin", "main")

    def live_snapshot(self):
        head = git(self.live, "rev-parse", "HEAD").stdout.strip()
        files = {f: open(os.path.join(self.live, f), "rb").read() for f in ("data.json", "index.html", "dashboard.html")}
        return head, files

    # tests
    def test_diverged_live_main_still_collects_and_deploys_on_remote_latest(self):
        self.diverge_live()
        before = self.live_snapshot()
        rc = self.run_script(self.ok_cmd())
        self.assertEqual(rc, 0, open(self.state + "/status.json").read() if os.path.exists(self.state + "/status.json") else "")
        d = self.remote_data()
        self.assertEqual(d["kr"]["flows"]["src_date_iso"], "2026-11-16")
        self.assertEqual(d["kr"]["flows"]["window"]["trading_days"], 20)
        self.assertEqual(d["us"]["vix"]["num"], 99.9)       # GitHub writer 변경 보존
        self.assertNotEqual(d["kr"]["fx"]["num"], 1360)     # 로컬 미푸시 커밋은 체리픽되지 않음
        self.assertEqual(self.live_snapshot(), before)      # 운영 작업본 무변경
        st = self.status()
        self.assertEqual(st["last_result"], "pushed")
        self.assertEqual(st["last_deploy_commit"], self.remote_head())
        html = sh(["git", "--git-dir", self.remote, "show", "main:index.html"]).stdout
        self.assertIn("수급 기준일", html)

    def test_push_failure_has_own_exit_and_keeps_artifact(self):
        hook = os.path.join(self.remote, "hooks", "pre-receive")
        open(hook, "w").write("#!/bin/sh\necho 'remote: Internal Server Error' >&2\nexit 1\n"); os.chmod(hook, 0o755)
        head0 = self.remote_head()
        rc = self.run_script(self.ok_cmd())
        self.assertEqual(rc, 21)
        self.assertEqual(self.remote_head(), head0)
        st = self.status()
        self.assertEqual(st["last_result"], "push_failed")
        self.assertNotIn("last_deploy_success_at", st)
        self.assertTrue(os.path.getsize(st["last_artifact"]) > 0)
        art = json.load(open(os.path.join(self.state, "latest_collect.json")))
        self.assertEqual(art["fields"]["kr.flows"]["src_date_iso"], "2026-11-16")
        self.assertEqual(git(self.live, "status", "--porcelain").stdout.strip(), "")

    def test_collect_failure_preserves_old_flows_and_marks_failed(self):
        old = self.remote_data()["kr"]["flows"]
        rc = self.run_script(f'"{sys.executable}" "{self.tmp}/fake_fail.py" "$1"')
        self.assertEqual(rc, 11)
        f = self.remote_data()["kr"]["flows"]
        for k in ("num", "note", "src_date"):
            self.assertEqual(f[k], old[k])
        self.assertEqual(f["collect_status"], "failed")
        html = sh(["git", "--git-dir", self.remote, "show", "main:index.html"]).stdout
        self.assertIn("flows-warn", html)

    def test_lock_prevents_concurrent_run(self):
        os.makedirs(os.path.join(self.state, "run.lock"))
        open(os.path.join(self.state, "run.lock", "pid"), "w").write(str(os.getpid()))
        head0 = self.remote_head()
        rc = self.run_script(self.ok_cmd())
        self.assertEqual(rc, 30)
        self.assertEqual(self.remote_head(), head0)

    def test_dead_lock_is_reclaimed(self):
        os.makedirs(os.path.join(self.state, "run.lock"))
        open(os.path.join(self.state, "run.lock", "pid"), "w").write("999999")
        self.assertEqual(self.run_script(self.ok_cmd()), 0)

    def test_old_live_lock_is_never_stolen(self):
        lock = os.path.join(self.state, "run.lock")
        os.makedirs(lock)
        open(os.path.join(lock, "pid"), "w").write(str(os.getpid()))
        os.utime(lock, (1, 1))
        before = self.remote_head()
        self.assertEqual(self.run_script(self.ok_cmd()), 30)
        self.assertTrue(os.path.isdir(lock))
        self.assertEqual(open(os.path.join(lock, "pid")).read(), str(os.getpid()))
        self.assertEqual(self.remote_head(), before)

    def test_unknown_old_lock_fails_closed(self):
        lock = os.path.join(self.state, "run.lock")
        os.makedirs(lock); os.utime(lock, (1, 1))
        self.assertEqual(self.run_script(self.ok_cmd()), 30)
        self.assertTrue(os.path.isdir(lock))

    def test_cleanup_does_not_remove_replacement_lock(self):
        lock = os.path.join(self.state, "run.lock")
        cmd = f'rm -rf "{lock}"; mkdir "{lock}"; printf other-owner > "{lock}/owner"; printf {os.getpid()} > "{lock}/pid"; exit 11'
        self.assertEqual(self.run_script(cmd), 11)
        self.assertEqual(open(os.path.join(lock, "owner")).read(), "other-owner")

    def test_kernel_lock_survives_helper_and_serializes_stale_reclaim(self):
        lock = os.path.join(self.state, "run.lock")
        os.makedirs(lock); open(os.path.join(lock, "pid"), "w").write("999999")
        ready = os.path.join(self.tmp, "collector-ready")
        release = os.path.join(self.tmp, "collector-release")
        cmd = f'touch "{ready}"; while [ ! -e "{release}" ]; do sleep 0.05; done; ' + self.ok_cmd()
        first = subprocess.Popen(["bash", SCRIPT], env=self.env(cmd), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 10
            while not os.path.exists(ready) and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue(os.path.exists(ready))
            self.assertEqual(self.run_script(self.ok_cmd()), 30)
            open(release, "w").close()
            self.assertEqual(first.wait(timeout=20), 0)
        finally:
            open(release, "w").close()
            first.wait(timeout=20)

    def assert_conflict_collects_without_push(self):
        before = self.remote_head()
        self.assertEqual(self.run_script(self.ok_cmd()), 23)
        self.assertEqual(self.remote_head(), before)
        self.assertEqual(self.status()["last_result"], "conflict_blocked")
        self.assertTrue(os.path.getsize(self.status()["last_artifact"]) > 0)

    def test_git_operation_markers_block_deployment_not_collection(self):
        for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "REBASE_HEAD", "rebase-merge", "rebase-apply", "sequencer"):
            with self.subTest(marker=marker):
                path = os.path.join(self.live, ".git", marker)
                if marker in ("rebase-merge", "rebase-apply", "sequencer"):
                    os.mkdir(path)
                else:
                    open(path, "w").close()
                try: self.assert_conflict_collects_without_push()
                finally:
                    if os.path.isdir(path): os.rmdir(path)
                    else: os.unlink(path)

    def test_unmerged_index_without_marker_blocks_deployment(self):
        head = git(self.live, "rev-parse", "HEAD:data.json").stdout.strip()
        git(self.live, "update-index", "--force-remove", "data.json")
        subprocess.run(["git", "-C", self.live, "update-index", "--index-info"],
                       input=f"100644 {head} 1\tdata.json\n100644 {head} 2\tdata.json\n", text=True, check=True)
        self.assert_conflict_collects_without_push()

    def test_conflict_appearing_during_collection_blocks_push(self):
        cmd = self.ok_cmd() + f'; touch "{self.live}/.git/CHERRY_PICK_HEAD"'
        before = self.remote_head()
        self.assertEqual(self.run_script(cmd), 23)
        self.assertEqual(self.remote_head(), before)
        self.assertTrue(os.path.exists(os.path.join(self.state, "latest_collect.json")))

    def test_github_writer_race_retries_on_fresh_remote(self):
        # 수집 도중 GitHub writer가 원격을 앞서가게 만든다 → 첫 push 거절(non-ff) → 재fetch 후 성공
        race = (f'git -C "{self.other}" pull -q --ff-only origin main && '
                f'"{sys.executable}" -c "import json;p=\'{self.other}/data.json\';d=json.load(open(p));'
                f'd[\'us\'][\'vix\'][\'num\']=77.7;json.dump(d,open(p,\'w\'),ensure_ascii=False,indent=2)" && '
                f'git -C "{self.other}" -c user.name=t -c user.email=t@t commit -q -am race && '
                f'git -C "{self.other}" push -q origin main && ' + self.ok_cmd())
        rc = self.run_script(race)
        self.assertEqual(rc, 0)
        d = self.remote_data()
        self.assertEqual(d["us"]["vix"]["num"], 77.7)
        self.assertEqual(d["kr"]["flows"]["src_date_iso"], "2026-11-16")
        logs = "".join(open(os.path.join(self.tmp, "logs", f)).read() for f in os.listdir(os.path.join(self.tmp, "logs"))
                       if f.endswith(".log"))
        self.assertIn("push 실패 (attempt 1/", logs)

    def test_second_run_same_data_does_not_break(self):
        self.assertEqual(self.run_script(self.ok_cmd()), 0)
        self.assertEqual(self.run_script(self.ok_cmd()), 0)
        self.assertEqual(self.remote_data()["kr"]["flows"]["src_date_iso"], "2026-11-16")

    def test_no_force_push_in_script(self):
        body = open(SCRIPT).read()
        push_lines = [l for l in body.splitlines() if " push " in l and not l.strip().startswith("#")]
        self.assertTrue(push_lines)
        for l in push_lines:
            self.assertNotIn("--force", l)
            self.assertNotIn("+HEAD", l)
            self.assertNotIn(" -f ", l)

    def test_credentials_not_sourced_into_parent_shell(self):
        body = open(SCRIPT).read()
        self.assertIn('( set -a; . "$ENV_FILE"; set +a; exec', body)
        self.assertNotIn('\nsource "$ENV_FILE"', body)


if __name__ == "__main__":
    unittest.main()
