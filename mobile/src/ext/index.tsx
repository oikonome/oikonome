import type { ComponentType } from "react";

// The slot an operator's client add-on fills. A store build applies the
// add-on over this directory (and adds its own screens under src/app/)
// before building; nothing in the product imports the add-on's files by
// name — only this object. In the public tree the slot is a household's
// own build of the app for its own instance: no plan, no store, no
// lockout beyond the two every instance has, and an allowance that is
// just its numbers.

export type Lockout = {
  // matched against the sentence /api/me answered with while the
  // household is frozen (the server's lockout table supplies it)
  matches: (message: string) => boolean;
  // the whole screen — every other call is refused in that state; the
  // product passes its sign-out
  Screen: ComponentType<{ onOut: () => void }>;
};

export type AccountSection = {
  // the Settings section the add-on owns, for the owner
  label: string;
  // words the Settings search maps to this section
  keywords: string;
  // the section's card; it asks the server what to show
  Card: ComponentType;
};

export const ext = {
  // a distributed store build is the operator's; a household's own build
  // of the app never is
  STORE_BUILD: false,
  IAP_BUILD: false,
  // screens the add-on adds under src/app/ — each needs its Stack entry
  screens: [] as { name: string; title: string }[],
  account: null as AccountSection | null,
  // cards rendered above the account card inside its section
  settingsCards: [] as ComponentType[],
  lockouts: [] as Lockout[],
  // a write refused with 402: the add-on's dialog, or null for the
  // product's own — which states the fact and offers nothing to buy
  paywall: null as ((message: string) => void) | null,
  // how many of the institution allowance are spent, when the server
  // reports one; `full` when the add door is refusing
  allowanceNote: (used: number | null | undefined, cap: number | null | undefined,
                  full: boolean): string =>
    `${used} of ${cap} institutions connected.`
    + (full ? " Disconnect one below to free a slot." : ""),
  // links the More tab and the SMS consent copy show when the operator
  // publishes them
  legal: { privacy: null as string | null, terms: null as string | null },
  // hosts whose passkeys the iOS binary may hold — equal to
  // app.json ios.associatedDomains, which the add-on supplies too
  webcredentialHosts: [] as string[],
  // the operator's site, offered when a server that looks like theirs
  // cannot say how to get an account
  site: null as { url: string; looksOurs: (serverUrl: string) => boolean } | null,
};
