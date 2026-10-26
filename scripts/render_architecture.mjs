// Optional documentation build; business services do not depend on this package.
import fs from 'node:fs/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const modulePath = process.argv[2];
if (!modulePath) throw new Error('Provide the installed @viz-js/viz dist/viz.js path');
const { instance } = await import(pathToFileURL(path.resolve(modulePath)).href);
const source = await fs.readFile(path.join(root, 'docs/architecture.mmd'), 'utf8');
const quote = value => JSON.stringify(value);
const dot = ['digraph architecture {',
  'graph [rankdir=TB, compound=true, nodesep=0.35, ranksep=0.65, pad=0.35, bgcolor="white", fontname="Arial", label="WeKnora 企微机器人5 · 本地与外部服务架构", labelloc=t, fontsize=24];',
  'node [shape=box, style="rounded,filled", fillcolor="#ffffff", color="#64748b", fontname="Arial", fontsize=14, margin="0.22,0.14"];',
  'edge [color="#64748b", fontcolor="#334155", fontname="Arial", fontsize=11, arrowsize=0.65];'];
const edges = [];
let nodes = 0;
for (const raw of source.split('\n')) {
  const line = raw.trim();
  let match = line.match(/^subgraph ([A-Z_]+)\["(.*)"\]$/);
  if (match) {
    const fill = { EXT: '#eef2ff', MAC: '#f8fafc', HOST: '#f0fdf4', DOCKER: '#eff6ff' }[match[1]];
    dot.push(`subgraph cluster_${match[1]} { label=${quote(match[2])}; style="rounded,filled"; color="#94a3b8"; fillcolor=${quote(fill)}; fontsize=18; margin=22;`);
    continue;
  }
  if (line === 'end') { dot.push('}'); continue; }
  match = line.match(/^([A-Z_]+)\[(\()?"(.*?)"(\))?\]$/);
  if (match) {
    const label = match[3].replaceAll('<br/>', '\n');
    dot.push(`${match[1]} [label=${quote(label)}${match[2] ? ', shape=cylinder' : ''}];`);
    nodes++;
    continue;
  }
  match = line.match(/^([A-Z_]+)\s+(<-->|-->|-\.->)(?:\|"([^"]*)"\|)?\s+([A-Z_]+)$/);
  if (match) {
    const [, from, arrow, label, to] = match;
    const attributes = [label ? `label=${quote(label)}` : '', arrow === '<-->' ? 'dir=both' : '', arrow === '-.->' ? 'style=dashed' : ''].filter(Boolean).join(', ');
    edges.push(`${from} -> ${to} [${attributes}];`);
  }
}
dot.push(...edges);
dot.push('WD WX MODEL ER SPEECH CRM [fillcolor="#e0e7ff"];');
dot.push('WORKER COMP REC ALERT COLLECT [fillcolor="#dcfce7"];');
dot.push('SOURCE MD STATE BACKUP SQLITE FILES LOG [fillcolor="#ffedd5"];');
dot.push('WEB NEO [fillcolor="#f3f4f6", style="rounded,filled,dashed"];');
dot.push('}');
const graph = dot.join('\n');
const renderer = await instance();
const svg = renderer.renderString(graph, { format: 'svg', engine: 'dot' });
await fs.writeFile(path.join(root, 'docs/architecture.dot'), graph + '\n');
await fs.writeFile(path.join(root, 'docs/architecture.svg'), svg);
console.log(JSON.stringify({ nodes, edges: edges.length, bytes: Buffer.byteLength(svg), graphvizVersion: renderer.graphvizVersion }));
