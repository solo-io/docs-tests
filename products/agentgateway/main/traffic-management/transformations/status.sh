YAMLTest -f - <<'EOF'
- name: verify response status is 401 when foo=bar query parameter is present
  http:
    url: "http://${INGRESS_GW_ADDRESS}:80/response-headers?foo=bar"
    method: GET
    headers:
      host: www.example.com
  source:
    type: local
  expect:
    statusCode: 401
- name: verify response status is 403 when foo=bar query parameter is absent
  http:
    url: "http://${INGRESS_GW_ADDRESS}:80/response-headers?foo=baz"
    method: GET
    headers:
      host: www.example.com
  source:
    type: local
  expect:
    statusCode: 403
EOF
