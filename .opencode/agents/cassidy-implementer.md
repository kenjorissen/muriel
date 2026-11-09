---
description: Cassidy implementation worker
mode: primary
permission:
  edit: allow
  bash: allow
  question: deny
  plan: deny
  task: deny
  external_directory:
    "*": deny
    "**/.git/cassidy/attempts/*": allow
---
