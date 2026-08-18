YAMLTest -f - <<'EOF'
- name: verify x-user-id-encoded response header contains base64 value
  http:
    url: "http://${INGRESS_GW_ADDRESS}:80/response-headers"
    method: GET
    headers:
      host: www.example.com
      x-user-id: user123
  source:
    type: local
  expect:
    statusCode: 200
    headers:
      - name: x-user-id-encoded
        comparator: equals
        value: dXNlcjEyMw==
EOF
