/** Static export: art-directed, locally optimized JPEGs with no image service. */
export function SpacePhoto({ name, alt, motion, hero = false }: {
  name: string; alt: string; motion: "drift" | "slow" | "spin"; hero?: boolean;
}) {
  return <picture className={`universe-photo universe-${motion}`}>
    <source media="(max-width: 640px)" srcSet={`/space/${name}-1200.jpg`} />
    <img src={`/space/${name}-2400.jpg`} alt={alt} loading={hero ? "eager" : "lazy"} fetchPriority={hero ? "high" : "auto"} decoding="async" />
  </picture>;
}
