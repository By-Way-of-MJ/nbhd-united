type BrandMarkProps = {
  size?: number;
  variant?: "dark" | "light";
  className?: string;
};

/** Approved nbhd-ios Brand/masters geometry. Decorative; label the containing link. */
export function BrandMark({ size = 32, variant = "dark", className = "" }: BrandMarkProps) {
  const small = size <= 32;
  const light = variant === "light";
  const nodes = small
    ? [[4.905, 23.255, 3.2], [13.225, 11.735, 2.35], [20.905, 18.775, 2.35], [27.945, 7.895, 2.35]]
    : [[148, 732, 60], [408, 372, 44], [648, 592, 44], [868, 252, 44]];
  const fills = ["fill-brand-purple", light ? "fill-brand-light-ink" : "fill-brand-white", "fill-brand-teal", "fill-brand-pink"];

  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox={small ? "0 0 32 32" : "0 0 1000 1000"}
      width={size}
      height={size}
      className={`shrink-0 ${className}`}
      aria-hidden="true"
      focusable="false"
    >
      <polyline
        points={small ? "4.905,23.255 13.225,11.735 20.905,18.775 27.945,7.895" : "148,732 408,372 648,592 868,252"}
        fill="none"
        className={light ? "stroke-brand-light-ink" : "stroke-brand-connector"}
        strokeOpacity={small ? 0.72 : light ? 0.65 : 0.42}
        strokeWidth={small ? 1.5 : 24}
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeDasharray={small ? undefined : "52 36"}
      />
      {nodes.map(([cx, cy, r], index) => (
        <circle key={index} cx={cx} cy={cy} r={r} className={fills[index]} />
      ))}
    </svg>
  );
}
