import type { ComponentType } from "react";

// The extension point a client add-on fills (an add-on is swapped in for
// this directory at build time, and may add screens under src/app/).
// Nothing in the product imports an add-on's files by name — only this
// object. Unextended, the slot adds no account section, no lockout beyond
// the two every instance has, and an allowance note that is just its
// numbers.

export type Lockout = {
  // matched against the sentence /api/me answered with while the
  // household is frozen (the server's lockout table supplies it)
  matches: (message: string) => boolean;
  // the whole screen — every other call is refused in that state; the
  // product passes its sign-out
  Screen: ComponentType<{ onOut: () => void }>;
};

export type AccountSection = {
  // the Settings section the add-on owns
  label: string;
  // words the Settings search maps to this section
  keywords: string;
  // the section's card; it asks the server what to show
  Card: ComponentType;
};

export const ext = {
  // screens the add-on adds under src/app/ — each needs its Stack entry
  screens: [] as { name: string; title: string }[],
  account: null as AccountSection | null,
  // cards rendered above the account card inside its section
  settingsCards: [] as ComponentType[],
  lockouts: [] as Lockout[],
  // a write refused with 402: the add-on's dialog, or null for the
  // product's own, which states the fact
  paywall: null as ((message: string) => void) | null,
  // how many of the institution allowance are spent, when the server
  // reports one; `full` when the add door is refusing
  allowanceNote: (used: number | null | undefined, cap: number | null | undefined,
                  full: boolean): string =>
    `${used} of ${cap} institutions connected.`
    + (full ? " A bank you disconnect keeps its slot for 30 days." : ""),
  // links the More tab and the SMS consent copy show when the operator
  // publishes them
  legal: { privacy: null as string | null, terms: null as string | null },
  // hosts whose passkeys the iOS binary may hold — equal to
  // app.json ios.associatedDomains, which the add-on supplies too
  webcredentialHosts: [] as string[],
  // a site offered when a server the add-on recognises cannot say how to
  // get an account
  site: null as { url: string; looksOurs: (serverUrl: string) => boolean } | null,
};
