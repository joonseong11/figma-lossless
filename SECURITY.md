# Security and evidence handling

Compiled bundles are evidence artifacts, not ordinary build output. They can contain:

- short-lived Figma MCP asset URLs;
- Figma node names, copy, screenshots, and reference code;
- local filesystem paths and MCP request identifiers;
- implementation screenshots and product-state fixtures.

`demo-output/`, common raw MCP response names, and report directories are ignored by Git. Keep them in access-controlled storage and do not attach them to public issues or CI logs.

Run `vendor-assets` promptly, then treat the resulting files and hashes according to the source Figma file's confidentiality. A SHA-256 hash proves byte identity; it does not grant redistribution rights.

The harness rejects bundle paths that are absolute or escape their artifact root, including symlink escapes. Asset vendoring accepts only canonical HTTPS Figma MCP asset URLs, revalidates redirects, limits bytes, and checks common image signatures and active SVG content. These controls reduce the attack surface but do not make arbitrary JSON or image files trustworthy; use OS-level isolation for untrusted bundles.

Report suspected vulnerabilities privately to the repository owner. Do not include proprietary Figma evidence in the report.
