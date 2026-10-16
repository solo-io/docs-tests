# WHAT THIS TEST VALIDATES:
#   * Both `credentials` fragments above are accepted once wrapped in the
#     gateways/routes scaffolding that the page tells the reader to add: the
#     additive form alongside a primary `key`, and the list on its own.
# WHAT THIS TEST DOES NOT VALIDATE (and why):
#   * That the extra credentials arrive at the backend -- covered on the
#     Kubernetes page for this method, which asserts all three against httpbin.
#     Asserting it twice would not test anything new about the data plane.
#   * The tunnel restriction in the note above -- requires config the page omits:
#     a tunnel-bound backend, which belongs to the backend tunnel guide.
echo dummy > subscription-key
credentials_case() {
  local name="$1"
  { cat <<'EOF'
# yaml-language-server: $schema=https://agentgateway.dev/schema/config
gateways:
  default:
    port: 3000
routes:
- backends:
  - host: backend.example.com:443
    policies:
      backendAuth:
EOF
    sed 's/^/        /'
  } > "config-credentials-$name.yaml"
  agentgateway -f "config-credentials-$name.yaml" --validate-only
}

credentials_case additive <<'EOF'
key:
  value: $MY_API_KEY
credentials:
- location:
    header:
      name: x-tenant-key
  key: $MY_TENANT_KEY
- location:
    queryParameter:
      name: subscription
  key:
    file: subscription-key
EOF

credentials_case alone <<'EOF'
credentials:
- location:
    header:
      name: x-tenant-key
  key: $MY_TENANT_KEY
EOF

echo "credentials configuration verified"
