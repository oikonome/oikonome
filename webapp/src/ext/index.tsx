import type { ComponentType, ReactNode } from "react";

// The slot an operator's client add-on fills. When an operator ships one,
// the image build replaces this whole directory with extras/webapp-ext/
// before the SPA is built (docker/Dockerfile); nothing in the product
// imports the add-on's files by name — only this object. In the public
// tree the slot is the household's own instance: no plan, no payment
// screen, no lockout beyond the two every instance has, and an allowance
// that is just its numbers.

export type Lockout = {
  // matched against the sentence /api/me answered with while the
  // household is frozen (the server's lockout table supplies it)
  matches: (message: string) => boolean;
  // the whole screen — every other call is refused in that state
  Page: ComponentType;
};

export type AccountSection = {
  // the Settings section the add-on owns. Its id is always "billing" so
  // /settings/billing keeps working as a deep link and a return URL.
  label: string;
  // words the Settings search maps to this section
  keywords: string;
  // whether this household has the section at all — a hook, since the
  // answer usually comes from the server
  useEnabled: () => boolean;
  // the one-line summary under the section's nav entry
  summary?: (plan: string | null | undefined) => { text: string } | undefined;
  // the section's owner-only card
  Card: ComponentType;
};

export const ext = {
  account: null as AccountSection | null,
  // cards the add-on adds to Settings → Users, under the household roster
  householdCards: [] as ComponentType[],
  lockouts: [] as Lockout[],
  // a write refused with 402: the toast's fallback sentence and where a
  // click on it lands, or null when the product has no such state
  paywall: null as { fallback: string; to: string } | null,
  // how many of the institution allowance are spent, when the server
  // reports one; `full` when the add door is refusing
  allowanceNote: (used: number, cap: number, full: boolean): ReactNode => (
    <><b>{used} of {cap}</b> institutions connected.
      {full ? " Disconnect one below to free a slot." : ""}</>
  ),
  // links the footer and consent copy show when the operator publishes them
  legal: { privacy: null as string | null, terms: null as string | null },
};
