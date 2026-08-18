YAMLTest -f - <<'EOF'
- name: verify numeric path segment is rewritten to /id
  http:
    url: "http://${INGRESS_GW_ADDRESS}:80/anything/users/12345"
    method: GET
    headers:
      host: www.example.com
  source:
    type: local
  expect:
    statusCode: 200
    bodyJsonPath:
      - path: "$.url"
        comparator: contains
        value: "/anything/users/id"
EOF
