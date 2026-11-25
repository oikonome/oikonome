// The ages the "Retire at each age" table opens on: today's age, the
// earliest age that works and the one just before it (where the answer
// turns), the age the stress test is read at, every fifth birthday, and the
// last age the plan looks at. Every other age is one tap away. Mirrored in
// mobile/src/lib/pure.ts (keyRetireAges), whose logic tests cover it.
export function keyRetireAges(ages: readonly number[],
                              earliest: number | null | undefined,
                              refAge?: number | null): Set<number> {
  const keep = new Set<number>();
  if (!ages.length) return keep;
  keep.add(ages[0]);
  keep.add(ages[ages.length - 1]);
  if (earliest != null) { keep.add(earliest); keep.add(earliest - 1); }
  if (refAge != null) keep.add(refAge);
  for (const a of ages) if (a % 5 === 0) keep.add(a);
  return new Set(ages.filter((a) => keep.has(a)));
}
