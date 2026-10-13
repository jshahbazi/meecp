# Website deployment

Published and verified on **2026-10-13**.

The public product website is [meecp.app](https://meecp.app). [www.meecp.app](https://www.meecp.app) serves the same page; the canonical URL, Open Graph metadata, and sitemap use the apex. The user selected this domain during website setup.

## Hosting

| Field | Value |
| --- | --- |
| Provider | Sites, with Cloudflare delivery |
| Project ID | `appgprj_6ace529c11348191a07d99efc6ec7c56` |
| Generated URL | https://meecp.arcanepine32.chatgpt.site |
| Audience | Public |
| Saved version | 2 |
| Version ID | `appgprj_6ace529c11348191a07d99efc6ec7c56~appgver_b10e4677c52081919b4f104cf549b256` |
| Deployment ID | `appgdep_6ace5851b7a08191a1ab647ae28a8121` |
| Source commit | `54f4db64bd83399247cf217f7360980b50818bcb` |
| Deployment status | `succeeded` |
| Apex domain ID | `appgdom_6ace56736b2481918ef1076c3bbf7023` |
| www domain ID | `appgdom_6ace56a0e5848191b178ac31680524b0` |
| Domain and TLS status | Active for both hostnames |

The application source is in [website/](../website/README.md). Build output, the isolated Sites source checkout, deployment archives, browser screenshots, and Cloudflare CLI cache are ignored. The Sites source commit is separate from the parent meecp repository commit. The published HTML, CSS, script, and assets match the checked-in website source.

## DNS

Cloudflare is authoritative for `meecp.app`, using `dom.ns.cloudflare.com` and `melissa.ns.cloudflare.com`. Both hostnames use DNS-only routing. The apex uses the two IPv4 targets returned by Sites; www uses the returned CNAME target. TTL is Cloudflare Auto.

| Type | Name | Target | Cloudflare record ID |
| --- | --- | --- | --- |
| `A` | `meecp.app` | `162.159.143.30` | `cb3bded87dc1d029c2de9217bcd17fe5` |
| `A` | `meecp.app` | `172.66.3.26` | `15f86daedf1f6f81779a6b45e10e51e0` |
| `TXT` | `_openai-site-verification.meecp.app` | Sites verification value | `2932e0afa42186f9e6b643174ccd7cac` |
| `TXT` | `_cf-custom-hostname.meecp.app` | Sites verification value | `096f7a299cfbe783770c1f42b50e9d5e` |
| `CNAME` | `www.meecp.app` | `custom-domains.chatgpt.site` | `0fa159179346b01ee3fd67aba4eb359a` |
| `TXT` | `_openai-site-verification.www.meecp.app` | Sites verification value | `bf4f8aaba4e790ecbce8e4726d10539e` |
| `TXT` | `_cf-custom-hostname.www.meecp.app` | Sites verification value | `bfb8991bb2bcadaa551e87a21a4ad479` |

The TXT values are public DNS verification records. Obtain their current values through Sites custom-domain details or the Cloudflare DNS zone when repairing a connection. Keep verification records present and preserve unrelated DNS records. The zone was empty before this deployment.

## Verification

- The static build completed, JavaScript syntax and Git whitespace checks passed, and all page assets loaded.
- The production page rendered at the generated origin and at `https://meecp.app/` in the browser.
- At a 390px mobile viewport, the document width remained 390px and all images loaded. Desktop and mobile layouts were visually inspected.
- Tool tabs responded to click, ArrowRight, and Home with the correct active panel and keyboard focus. Native FAQ disclosure expanded the release details.
- Ordinary HTTPS requests returned 200 for both hostnames with the expected page title. Sites reported both domains and their certificates active.
- Documentation links were checked against the repository. The page identifies the current public installer as `v1.0.0`, under the former `mcpme` name, and distinguishes current development features.

These checks cover the website. Product acceptance continues to be owned by the existing status and verification documents.

## Updating and recovery

Follow [the website publishing instructions](../website/README.md). Preserve the project ID and public audience, synchronize the exact source through the Sites source helper, then save and deploy the matching commit and archive. Reuse the existing project and custom-domain associations.

For a website rollback, select a previous saved Sites version and deploy it on the same project. The DNS records and hostname associations continue to route to that project's active version. A source-only Git revert needs a new matching build and publication.

