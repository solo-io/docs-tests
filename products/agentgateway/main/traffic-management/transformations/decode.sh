YAMLTest -f - <<'EOF'
- name: verify x-user-id-decoded response header contains plain-text value
  http:
    url: "http://${INGRESS_GW_ADDRESS}:80/response-headers"
    method: GET
    headers:
      host: www.example.com
      x-user-id-encoded: dXNlcjEyMw==
  source:
    type: local
  expect:
    statusCode: 200
    headers:
      - name: x-user-id-decoded
        comparator: equals
        value: user123
EOF
