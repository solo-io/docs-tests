# WHAT THIS TEST VALIDATES:
#   * The static-key backendAuth example config is accepted by agentgateway in
#     both the routing-based (gateways) and simplified MCP (mcp.policies) forms.
# WHAT THIS TEST DOES NOT VALIDATE (and why):
#   * The file-path, location, and passthrough snippets are field-reference
#     fragments with no `gateways:`, so they are not standalone configs as
#     written. The `credentials` fragments at the end of the page are covered by
#     a second test, which wraps each one in the missing scaffolding first.
#   * That a backend accepts any of these credentials -- requires config the page
#     omits: every example points at a placeholder host.
cat <<'EOF' > config.yaml
# yaml-language-server: $schema=https://agentgateway.dev/schema/config
gateways:
  default:
    port: 3000
routes:
- backends:
  - host: localhost:8080
    policies:
      backendAuth:
        key:
          value: $MY_API_KEY
EOF
agentgateway -f config.yaml --validate-only

cat <<'EOF' > config-mcp.yaml
# yaml-language-server: $schema=https://agentgateway.dev/schema/config
mcp:
  port: 3000
  policies:
    backendAuth:
      key:
        value: $MY_API_KEY
  targets:
  - name: everything
    stdio:
      cmd: npx
      args: ["@modelcontextprotocol/server-everything"]
EOF
agentgateway -f config-mcp.yaml --validate-only
