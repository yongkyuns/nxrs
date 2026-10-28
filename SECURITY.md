# Reporting security issues

Report suspected vulnerabilities privately through [GitHub private vulnerability
reporting](https://github.com/yongkyuns/nxrs/security/advisories/new). If that
repository feature is unavailable, contact the repository owner through their
[GitHub profile](https://github.com/yongkyuns) to establish a private channel;
do not post exploit details, credentials or private footage in a public issue.

Include the affected commit, target/profile, component, prerequisites, impact
and a minimal synthetic reproduction. Remove credentials, personal addresses,
footage and raw device logs. Do not test other people's devices or deployments.
There is no guaranteed response time or supported commercial release at present.

Security fixes target the current main branch. Development and qualification
images are not production security configurations. For deployment boundaries,
release gates and the FIPS decision, see [the threat model](docs/security.md).

The secret-scanning workflow checks available Git history on pull requests,
main pushes and weekly. It reports finding locations without secret values.
If a real credential is detected, revoke or rotate it through a private channel;
removing the current file does not remove it from Git history.
