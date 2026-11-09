---
description: Cassidy review worker
mode: primary
permission:
  edit: deny
  bash: allow
  question: deny
  plan: deny
  task: deny
  external_directory:
    "*": deny
    "**/.git/cassidy/attempts/*": allow
---
