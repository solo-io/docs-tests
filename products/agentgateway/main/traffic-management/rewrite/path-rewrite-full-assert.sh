YAMLTest -f - <<'EOF'
- name: path rewrite full - /headers rewrites to /anything
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
      - path: "$.url"
        comparator: contains
        value: "/anything"
EOF
