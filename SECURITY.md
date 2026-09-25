# Security

- No credentials live in this repository. Broker tokens are held in the operator's OS keychain; the
  unattended runner only ever has a read-only token. The write token is loaded only inside the
  interactive operator terminal, behind an OS prompt and a typed confirmation.
- The publisher builds public documents from allow-listed models and runs a leak scan (money
  amounts, account and order identifiers, paths, e-mails, tokens) plus gitleaks before every commit.
- If you find something sensitive in this repo, open an issue titled "security" without the content,
  and it will be removed and the relevant credential rotated.
