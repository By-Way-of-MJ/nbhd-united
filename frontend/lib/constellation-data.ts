import type { ConstellationData, ConstellationNode } from "./types";

// ── Tag-based clustering fallback ────────────────────────────────────────────

export function clusterByTags(nodes: ConstellationNode[]): { clusters: ConstellationData["clusters"]; clusterMap: Map<number, number> } {
  if (nodes.length === 0) return { clusters: [], clusterMap: new Map() };
  const tagCounts = new Map<string, number>();
  for (const n of nodes) for (const t of n.tags) tagCounts.set(t, (tagCounts.get(t) || 0) + 1);
  const seedTags = [...tagCounts.entries()].filter(([, c]) => c >= 2).sort((a, b) => b[1] - a[1]).slice(0, 8).map(([t]) => t);
  if (seedTags.length < 2) { seedTags.length = 0; seedTags.push(...[...tagCounts.entries()].sort((a, b) => b[1] - a[1]).slice(0, 6).map(([t]) => t)); }
  const clusterMap = new Map<number, number>();
  for (const n of nodes) { const idx = n.tags.findIndex((t) => seedTags.includes(t)); if (idx >= 0) clusterMap.set(n.id, seedTags.indexOf(n.tags[idx])); }
  for (const n of nodes) { if (!clusterMap.has(n.id) && seedTags.length > 0) clusterMap.set(n.id, n.id % seedTags.length); }
  const counts = new Map<number, number>(); const tagSets = new Map<number, Set<string>>();
  for (const [nid, cid] of clusterMap) { counts.set(cid, (counts.get(cid) || 0) + 1); const nd = nodes.find((x) => x.id === nid); if (nd) { const s = tagSets.get(cid) || new Set<string>(); nd.tags.forEach((t) => s.add(t)); tagSets.set(cid, s); } }
  return { clusters: seedTags.map((tag, i) => ({ id: i, label: tag.charAt(0).toUpperCase() + tag.slice(1).replace(/_/g, " "), count: counts.get(i) || 0, tags: [...(tagSets.get(i) || [])].slice(0, 5) })).filter((c) => c.count > 0), clusterMap };
}

/** The constellation as shown: real clusters, or tag groups when none have been computed yet. */
export function withTagClusters(rawData: ConstellationData): ConstellationData {
  if (rawData.clusters.length > 0 || !(rawData.nodes.length > 0 && rawData.nodes.every((n) => n.cluster_id == null))) return rawData;
  const { clusters, clusterMap } = clusterByTags(rawData.nodes);
  return { ...rawData, nodes: rawData.nodes.map((n) => { const cid = clusterMap.get(n.id); return cid != null ? { ...n, cluster_id: cid } : n; }), clusters };
}
