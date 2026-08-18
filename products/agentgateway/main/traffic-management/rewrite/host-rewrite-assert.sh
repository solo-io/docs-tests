YAMLTest -f - <<'EOF'
- name: host rewrite - rewrite.example rewrites host header to www.example.com
  retries: 1
  http:
    url: "http://${INGRESS_GW_ADDRESS}:80"
    path: /headers
    method: GET
    headers:
      host: "rewrite.example"
  source:
    type: local
  expect:
    statusCode: 200
    bodyJsonPath:
      - path: "$.headers.Host[0]"
        comparator: equals
        value: "www.example.com"
EOF
