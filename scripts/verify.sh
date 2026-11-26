#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
npm test
npm run scenarios
npm run demo

echo
echo 'All verification steps passed. Start the service with: npm start'
