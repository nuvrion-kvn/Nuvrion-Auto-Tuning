"""Isolated regressions: never source or execute the complete installer."""
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "nuvrion-auto-tuning.sh"
SOURCE = SCRIPT.read_text()


def function(name):
    match = re.search(r"^" + re.escape(name) + r"\(\) \{\n.*?^\}", SOURCE, re.M | re.S)
    if not match:
        raise AssertionError(name)
    return match.group()


def bash(names, body, **kwargs):
    code = "set -Eeuo pipefail\n" + "\n".join(function(n) for n in names) + "\n" + body
    return subprocess.run(["bash", "-c", code], text=True, capture_output=True,
                          timeout=8, **kwargs)


class AuditTests(unittest.TestCase):
    def test_syntax(self):
        subprocess.run(["bash", "-n", str(SCRIPT)], check=True)

    def test_nofile(self):
        for pair, expected in [("1024/unlimited", 1), ("unlimited/1024", 1),
                               ("1048576/unlimited", 0), ("infinity/infinity", 0),
                               ("1048576/1048576", 0), ("x/1048576", 1),
                               ("18446744073709551617/1048576", 1), ("08/09", 1)]:
            with self.subTest(pair=pair):
                r = bash(["nofile_pair_ok"], 'NOFILE_TARGET=1048576; nofile_pair_ok "$PAIR"',
                         env={**os.environ, "PAIR": pair})
                self.assertEqual(r.returncode, expected, r.stderr)

    def test_atomic_failures_preserve_file(self):
        for fail in ["mktemp", "cat", "chmod", "mv"]:
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as d:
                target = Path(d) / "config"
                target.write_text("original\n")
                r = bash(["register_temp_file", "atomic_write_file"], f'''
RUNTIME_TEMP_FILES=()
{fail}() {{ return 1; }}
if atomic_write_file "$TARGET" 0600 <<<replacement; then exit 77; fi
''', env={**os.environ, "TARGET": str(target)})
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(target.read_text(), "original\n")
                self.assertEqual(list(Path(d).glob("*.nuvrion.*")), [])

    def test_atomic_success_and_repeat(self):
        with tempfile.TemporaryDirectory() as d:
            target = Path(d) / "config"
            r = bash(["register_temp_file", "atomic_write_file"], '''
RUNTIME_TEMP_FILES=()
atomic_write_file "$TARGET" 0600 <<<replacement
atomic_write_file "$TARGET" 0600 <<<replacement
''', env={**os.environ, "TARGET": str(target)})
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(target.read_text(), "replacement\n")
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)

    def test_ss_failure_not_empty_success(self):
        for name in ["collect_public_tcp_ports", "collect_public_low_udp_ports"]:
            r = bash([name], f'SS_BIN=false; PORT_LOW=10240; if result=$({name}); then exit 77; fi')
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_port_decimal_and_bounds(self):
        r = bash(["merge_reserved_ports"], 'merge_reserved_ports "08,09,010-012,0,65536,18446744073709551617"')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "8-12")
        r = bash(["merge_reserved_ports", "filter_port_spec_to_range"],
                 'PORT_LOW=8; PORT_HIGH=12; filter_port_spec_to_range "08-010,12,18446744073709551617"')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "8-10,12")

    def test_ssh_session_port(self):
        r = bash(["get_sshd_ports"], 'SSHD_BIN=""; SS_BIN=""; SSH_CONNECTION="192.0.2.1 50000 192.0.2.2 22222"; get_sshd_ports')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("22222", r.stdout.splitlines())

    def test_apt_timeout_no_retry(self):
        r = bash(["apt_run", "apt_install_zram_packages"], '''
info() { :; }; warn() { :; }; refresh_zram_bins() { :; }
mock_timeout() { echo attempt; return 124; }
APT_INTERRUPTED=0; INSTALL_ZRAM_PACKAGES=1; APT_GET_BIN=unused; TIMEOUT_BIN=mock_timeout
if apt_install_zram_packages test-package; then exit 77; fi
[[ $APT_INTERRUPTED == 1 ]]
if apt_install_zram_packages another-package; then exit 78; fi
''')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(), ["attempt"])

    def test_zram_no_reset_and_skip_final(self):
        # Structural checks, not execution against host sysfs/systemd.
        self.assertNotIn('echo 1 >"\\$SYS/reset"', SOURCE)
        self.assertIn('if [[ \\$current != 0 ]]; then', SOURCE)
        self.assertIn('final_skip "ZRAM" "$ZRAM_STATUS"', SOURCE)
        self.assertNotIn('enable --now nuvrion-zram.service', SOURCE)

    def test_no_removed_traffic_installer(self):
        self.assertNotRegex(SOURCE.lower(), r"traffic[-_ ]?control|trafficguard")

    def test_atomic_calls_checked(self):
        for line in SOURCE.splitlines():
            if re.match(r'\s*(?:atomic_write_file|\} \| atomic_write_file) ', line):
                self.assertRegex(line, r'\|\| (return|exit) 1$', line)

    def test_compose_failure_attempts_rollback(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            (base / "compose.yml").write_text("services: {}\n")
            r = bash(["fix_remnanode_nofile_via_compose", "register_temp_file", "atomic_write_file"], '''
STATE_DIR=$TEST_DIR; RUN_ID=test; NOFILE_TARGET=1048576
TIMEOUT_BIN=mock; DOCKER_BIN=unused; NUVRION_ALLOW_NODE_RECREATE=1
RUNTIME_TEMP_FILES=()
warn() { :; }; info() { :; }; ok() { :; }; conflict() { :; }
wait_remnanode_running() { return 0; }
mock() {
    shift 2
    if [[ $1 == inspect ]]; then
        case "$*" in
            *working_dir*) echo "$TEST_DIR" ;;
            *config_files*) echo "$TEST_DIR/compose.yml" ;;
            *compose.service*) echo remnanode ;;
            *compose.project*) echo test ;;
        esac
    elif [[ $* == *' up '* ]]; then
        echo up >>"$TEST_DIR/calls"
        [[ $(wc -l <"$TEST_DIR/calls") -gt 1 ]]
    elif [[ ${*: -1} == config ]]; then
        printf 'services: {}\n'
    fi
}
if fix_remnanode_nofile_via_compose; then exit 77; fi
[[ $(wc -l <"$TEST_DIR/calls") == 2 ]]
[[ -f $TEST_DIR/docker/test-rollback-test.yml ]]
[[ ! -e $TEST_DIR/docker/test-nofile.override.yml ]]
''', env={**os.environ, "TEST_DIR": d})
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_release_guards(self):
        self.assertIn('NUVRION_SSH_KEY_LOGIN_CONFIRMED', SOURCE)
        self.assertIn('LimitNOFILESoft --value', SOURCE)
        self.assertNotIn('RPS_GLOBAL > 0', SOURCE)
        self.assertIn('elif rps_current_active;', SOURCE)
        self.assertIn('write_nuvrion_zram_helper || exit 1', SOURCE)

    def test_embedded_bash_syntax(self):
        blocks = re.findall(r"<<'?([A-Z][A-Z0-9_]*)'?[^\n]*\n(#!/usr/bin/env bash\n.*?)^\1$",
                            SOURCE, re.M | re.S)
        self.assertGreaterEqual(len(blocks), 5)
        for delimiter, body in blocks:
            with self.subTest(delimiter=delimiter):
                # Syntax only: do not evaluate or run generated helpers.
                r = subprocess.run(["bash", "-n"], input=body.replace(r"\$", "$"),
                                   text=True, capture_output=True)
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_panel_ip_eof(self):
        master, slave = os.openpty()
        try:
            code = function("prompt_panel_ips_required") + '''
C_BOLD=""; C_YELLOW=""; C_RESET=""
choice_hint_skip() { :; }; warn() { :; }
prompt_panel_ips_required ""
rc=$?
[[ $rc == 2 && $PANEL_SETUP_SKIPPED == 1 ]]
'''
            proc = subprocess.Popen(["bash", "-c", code], stdin=slave,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            os.write(master, b"\x04")
            try:
                out, err = proc.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                self.fail("EOF caused a prompt loop")
            self.assertEqual(proc.returncode, 0, out + err)
        finally:
            os.close(slave)
            os.close(master)

    def test_panel_skip_return_code(self):
        r = bash(["resolve_panel_identity", "valid_tcp_port"], '''
PANEL_PORT_ENV=""; C_MAGENTA=""; C_RESET=""; SSH_PORTS=()
get_saved_panel_port() { :; }; get_saved_panel_ips() { :; }
collect_rw_node_listener_ports() { echo 2222; }
collect_grouped_firewall_candidates() { printf '2222\t192.0.2.1\tufw\n'; }
public_tcp_port_listening() { return 0; }
prompt_choice_yes_no_skip() { return 0; }
prompt_panel_ips_required() { return 2; }
if resolve_panel_identity; then exit 77; else [[ $? == 2 ]]; fi
''')
        self.assertEqual(r.returncode, 0, r.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
