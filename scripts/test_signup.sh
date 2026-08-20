#!/usr/bin/env bash
# Create a driver account from four fields, then log in with it.
#
#   ./scripts/test_signup.sh
#   HOST=http://127.0.0.1:8016 ./scripts/test_signup.sh
#
# The email and phone number are randomised on every run, so a repeat never
# collides with the account the last run created. To check what ended up in
# the database, or to clear a test account out:
#
#   bench --site <site> console
#   >>> exec(open("apps/erpera_driver_app/scripts/signup_check.py").read())
#   >>> report("<the email printed below>", "<the phone printed below>")
#   >>> cleanup("<the email>", confirm=False)

set -uo pipefail
HOST="${HOST:-http://127.0.0.1:8016}"
N=$((RANDOM % 9000 + 1000))
EMAIL="${EMAIL:-driver.test$N@example.com}"
MOBILE="${MOBILE:-9$(printf '%09d' $((RANDOM * RANDOM % 1000000000)))}"
PASSWORD="${PASSWORD:-Str0ng-Pass-2026}"

echo "email : $EMAIL"
echo "phone : $MOBILE"
echo
echo "== signup — expects a new user, employee and driver"
curl -sS -X POST "$HOST/api/method/erpera_driver_app.api.signup.signup" \
  -H 'Content-Type: application/json' \
  -d "{\"full_name\":\"Test Driver $N\",\"mobile_no\":\"$MOBILE\",
       \"email\":\"$EMAIL\",\"password\":\"$PASSWORD\"}" | python3 -m json.tool

echo
echo "== login — expects api_key and api_secret"
curl -sS -X POST "$HOST/api/method/erpera_driver_app.api.auth.driver_login" \
  -H 'Content-Type: application/json' \
  -d "{\"usr\":\"$EMAIL\",\"pwd\":\"$PASSWORD\"}" | python3 -m json.tool
