#!/usr/bin/env python3
"""The schema check's Helm values check (solo-io/docs-tests#19).

The product charts ship no values.schema.json, so the check builds one from the
chart's values.yaml and templates. It must accept everything the chart takes,
including free-form maps and options values.yaml only shows commented out.
"""
import importlib.util
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from doc_test_helm_values import check_blocks, load_chart, parse_block, split_key, unknown_keys  # noqa: E402

VALUES = """\
resources:
  requests:
    cpu: 100m
podAnnotations:
  prometheus.io/scrape: "true"
labels: {}
controller:
  image:
    pullPolicy: ""
  logLevel: info
  service:
    annotations:
      a: b
# validation:
#   level: standard
"""

DEPLOYMENT = """\
metadata:
  annotations:
    {{- toYaml .Values.podAnnotations | nindent 4 }}
spec:
  resources: {{ toYaml .Values.resources | nindent 4 }}
  {{- with .Values.controller.service.annotations }}
  annotations:
    {{- toYaml . | nindent 4 }}
  {{- end }}
  level: {{ .Values.validation.level }}
  extra: {{ index .Values "controller" "extraFlag" }}
"""


def chart(tmp, name="demo", values=VALUES, templates=DEPLOYMENT, overlay=None):
    d = pathlib.Path(tmp) / name
    (d / "templates").mkdir(parents=True)
    (d / "Chart.yaml").write_text(f"name: {name}\n")
    if values is not None:
        (d / "values.yaml").write_text(values)
    (d / "templates" / "deployment.yaml").write_text(templates)
    return load_chart(d, overlay)


def block(content, line=1, path="page.md"):
    return SimpleNamespace(content=content, start_line=line, file_path=pathlib.Path(path))


def findings(blocks, charts):
    checked, findings = check_blocks(blocks, charts, lambda b: f"{b.file_path}:{b.start_line}")
    return checked, [f.message for f in findings]


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.chart = chart(self.tmp)

    def unknown(self, values):
        return unknown_keys(self.chart.schema, values)

    def test_misspelled_and_removed_keys(self):
        self.assertEqual(self.unknown({"controller": {"logLvl": "debug"}}), ["controller.logLvl"])
        self.assertEqual(self.unknown({"agentgatewayModels": {"enabled": True}}), ["agentgatewayModels"])

    def test_free_form_values_are_open(self):
        # toYaml'd, `with` + toYaml ., or empty by default: Kubernetes fields, not chart options.
        self.assertEqual(self.unknown({
            "resources": {"limits": {"memory": "1Gi"}},
            "podAnnotations": {"other": "x"},
            "labels": {"team": "a"},
            "controller": {"service": {"annotations": {"x": "y"}}},
        }), [])

    def test_keys_only_templates_read_are_accepted(self):
        # validation.level is commented out in values.yaml; extraFlag is read with `index`.
        self.assertEqual(self.unknown({"validation": {"level": "strict"}, "controller": {"extraFlag": 1}}), [])

    def test_chart_with_no_values_takes_none(self):
        crds = chart(self.tmp, "crds", values=None, templates="kind: CustomResourceDefinition\n")
        self.assertEqual(unknown_keys(crds.schema, {"controller": {"image": {}}}), ["controller"])

    def test_value_passed_to_a_component_is_open(self):
        c = chart(self.tmp, "omits", values="tokenExchange:\n  enabled: false\n  configSecret:\n    name: \"\"\n",
                  templates='value: {{ omit .Values.tokenExchange "configSecret" | toJson | quote }}\n')
        self.assertEqual(unknown_keys(c.schema, {"tokenExchange": {"issuer": "x", "database": {"type": "postgres"}}}), [])

    def test_subchart_values_go_under_its_alias(self):
        parent = pathlib.Path(self.tmp) / "parent"
        sub = parent / "charts" / "parent-ui"
        (sub / "templates").mkdir(parents=True)
        (sub / "Chart.yaml").write_text("name: parent-ui\n")
        (sub / "values.yaml").write_text("service:\n  type: ClusterIP\n")
        (parent / "templates").mkdir()
        (parent / "Chart.yaml").write_text("name: parent\ndependencies:\n- name: parent-ui\n  alias: ui\n"
                                           "- name: not-vendored\n")
        (parent / "values.yaml").write_text("ui:\n  enabled: false\n")
        c = load_chart(parent)
        self.assertEqual(unknown_keys(c.schema, {
            "ui": {"enabled": True, "service": {"type": "LoadBalancer"}},
            "not-vendored": {"anything": 1},
            "global": {"imageRegistry": "r"},
        }), [])
        self.assertEqual(unknown_keys(c.schema, {"ui": {"service": {"typo": 1}}}), ["ui.service.typo"])

    def test_overlay_opens_a_value_and_flags_stale_entries(self):
        c = chart(self.tmp, "overlaid", overlay={"overlaid": {"open": ["controller.image", "gone.key", "labels"]}})
        self.assertEqual(unknown_keys(c.schema, {"controller": {"image": {"anything": 1}}}), [])
        # gone.key isn't in the chart; labels is already open.
        self.assertEqual(c.stale_overlay, ["gone.key", "labels"])


class ParseTests(unittest.TestCase):
    def test_split_key(self):
        self.assertEqual(split_key(r"podAnnotations.prometheus\.io/scrape"), ["podAnnotations", "prometheus.io/scrape"])
        self.assertEqual(split_key("a.list[0].b"), ["a", "list"])

    def test_set_flags_continuations_and_chart(self):
        cmds, _ = parse_block(
            "helm upgrade -i agw oci://cr.example.dev/charts/demo \\\n"
            "  --namespace ns --version 1.0.0 \\\n"
            "  --set a=1,b={x,y} \\\n"
            "  --set-json 'c={\"d\":1}' \\\n"
            "  --wait\n"
        )
        self.assertEqual(len(cmds), 1)
        self.assertEqual(cmds[0].chart_ref, "oci://cr.example.dev/charts/demo")
        self.assertEqual(cmds[0].sets, [("--set", "a=1"), ("--set", "b={x,y}"), ("--set-json", 'c={"d":1}')])

    def test_heredoc_writes_and_stdin(self):
        cmds, writes = parse_block(
            "cat <<'EOF' > values.yaml\nhelm install x y\nEOF\n"
            "helm upgrade -i agw demo --values - <<EOF\nlogLevel: debug\nEOF\n"
        )
        # The `helm install` inside the heredoc body is data, not a command.
        self.assertEqual([c.chart_ref for c in cmds], ["demo"])
        self.assertEqual(cmds[0].stdin_values, "logLevel: debug\n")
        self.assertEqual([w[1] for w in writes], ["values.yaml"])


class CheckBlocksTests(unittest.TestCase):
    def setUp(self):
        self.charts = {"demo": chart(tempfile.mkdtemp())}

    def test_set_on_the_product_chart(self):
        checked, msgs = findings([block("helm upgrade -i agw oci://r/demo --set controller.logLvl=debug\n")],
                                 self.charts)
        self.assertEqual(checked, 1)
        self.assertEqual(msgs, ["chart demo has no value 'controller.logLvl' (--set controller.logLvl)"])

    def test_other_charts_are_skipped(self):
        checked, msgs = findings([
            block("helm install otel open-telemetry/opentelemetry-collector --set mode=deployment\n"),
            # Same chart name, but from a --repo: a third-party chart.
            block("helm install x demo --repo https://charts.example.com --set nope=1\n", line=5),
        ], self.charts)
        self.assertEqual((checked, msgs), (0, []))

    def test_values_file_from_each_tab_goes_to_the_next_command(self):
        tab_a = block("cat <<'EOF' > values.yaml\ncontroller:\n  logLevel: debug\nEOF\n", line=10)
        tab_b = block("cat <<'EOF' > values.yaml\ncontroller:\n  bogus: 1\nEOF\n", line=20)
        install = block("helm upgrade -i agw oci://r/demo -f values.yaml\n", line=30)
        # A later values.yaml for another chart must not be checked against demo.
        other = block("cat > values.yaml <<EOF\nmode: deployment\nEOF\nhelm install o grafana/loki -f values.yaml\n", line=40)
        checked, msgs = findings([other, install, tab_b, tab_a], self.charts)
        self.assertEqual(checked, 2)
        self.assertEqual(msgs, ["chart demo has no value 'controller.bogus' (values file values.yaml)"])


@unittest.skipUnless(importlib.util.find_spec("jsonschema"), "jsonschema is not installed")
class EndToEndTests(unittest.TestCase):
    """Through doc_test_schema_check.main(): a value the chart doesn't have fails
    the scenario, and Helm values alone don't count toward a pass."""

    def test_bad_value_fails_the_scenario(self):
        import test_schema_check as t
        base = t.Base()
        base.setUp()
        chart_dir = pathlib.Path(tempfile.mkdtemp())
        chart(chart_dir)
        key = base.write("main", t.page(t.policy_block("timeout: 10s") + "\n\n```sh\nhelm install agw oci://r/demo "
                                        "--set controller.logLvl=debug\n```"))
        code, tests = base.run_check(str(base.new), f"--helm-chart=main={chart_dir / 'demo'}")
        self.assertEqual(code, 1)
        self.assertEqual(tests[key]["status"], "failed")
        self.assertIn("chart demo has no value 'controller.logLvl'", tests[key]["error"])

    def test_helm_values_alone_do_not_pass(self):
        import test_schema_check as t
        base = t.Base()
        base.setUp()
        chart_dir = pathlib.Path(tempfile.mkdtemp())
        chart(chart_dir)
        key = base.write("main", t.page("```sh {paths=\"policy\"}\nhelm install agw oci://r/demo "
                                        "--set controller.logLevel=debug\n```"))
        code, tests = base.run_check(str(base.new), f"--helm-chart=main={chart_dir / 'demo'}")
        self.assertEqual(code, 1)
        self.assertIn("found no recognizable custom resource manifest", tests[key]["error"])


if __name__ == "__main__":
    unittest.main()
