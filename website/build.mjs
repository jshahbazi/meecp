import { cp, mkdir, rm } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(fileURLToPath(import.meta.url));
const output = join(root, 'dist');
await rm(output, { recursive: true, force: true });
await mkdir(output, { recursive: true });
for (const name of ['index.html', 'styles.css', 'site.js', 'robots.txt', 'sitemap.xml', 'assets']) {
  await cp(join(root, name), join(output, name), { recursive: true });
}
console.log('Static website built in dist/');
