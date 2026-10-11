#!/bin/bash
# login_check.sh <url> <password file> [user]: prints the HTTP status of a login (password is never printed)
URL="${1:?url}"; PWF="${2:?password file}"; U="${3:-admin}"
P="$(tr -d '\r\n' < "$PWF")"
curl -sk -o /dev/null -w '%{http_code}\n' -H 'Content-Type: application/json' \
  -d "$(printf '{"username":"%s","password":"%s"}' "$U" "$P")" "$URL/api/auth/login"
