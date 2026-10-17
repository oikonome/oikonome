import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Alert, Pressable, Text } from "react-native";

import { errText, type Me } from "../lib/api";
import { awaitsEmailConfirm } from "../lib/pure";
import { useSession } from "../lib/session";
import { C, mmddyy } from "../lib/theme";
import { Card, H } from "./ui";

/** Why the hosted bank door is closed to an unconfirmed address (the
 *  allowance is zero until an owner confirms): the note on the card above
 *  the door and the disabled button's explanation, so the refusal is read
 *  before the tap. The web's CONFIRM_TO_CONNECT, verbatim. */
export const CONFIRM_TO_CONNECT =
  "Confirm your email to connect a bank — the link is in your inbox.";

/** The way to ask for the confirmation mail again, at any account age —
 *  the web's VerifyEmailCard. The tabs banner waits out a grace period so
 *  a brand-new signup is not nagged on day one; this card does not,
 *  because a link that went to spam, expired, or was spent by a mail
 *  scanner leaves no other way to get a fresh one. Hosted only: there the
 *  household's mail goes to confirmed addresses alone. */
export function VerifyEmailCard({ me, note }: {
  me: Me | undefined;
  // what waiting on the address holds up, in the place the card sits
  note?: string;
}) {
  const { client } = useSession();
  const qc = useQueryClient();
  const resend = useMutation({
    mutationFn: () => client!.verifyResend(),
    onSuccess: (r) => {
      // already confirmed (the link was opened since): the refreshed
      // /api/me takes this card away
      if (r.verified) qc.invalidateQueries({ queryKey: ["me"] });
      Alert.alert(r.verified ? "Already confirmed."
                             : "Confirmation email sent.");
    },
    onError: (e) => Alert.alert("Resend failed", errText(e)),
  });
  if (!me || !awaitsEmailConfirm(me)) return null;
  return (
    <Card>
      <H>Confirm your email</H>
      <Text style={{ color: C.mut, fontSize: 13, lineHeight: 19 }}>
        {note ? `${note} ` : ""}We sent a link to {me.email}. No email?
        Check spam, or resend.
        {me.verify_deadline
          ? ` Unconfirmed accounts are frozen on ${
              mmddyy(me.verify_deadline)} and deleted a week later.`
          : ""}
      </Text>
      <Pressable style={{ alignSelf: "flex-start", marginTop: 8,
                          backgroundColor: C.accent, borderRadius: 8,
                          paddingHorizontal: 16, paddingVertical: 8,
                          opacity: resend.isPending ? 0.5 : 1 }}
                 disabled={resend.isPending}
                 onPress={() => resend.mutate()}>
        <Text style={{ color: C.text, fontSize: 13, fontWeight: "600" }}>
          {resend.isPending ? "sending…" : "Resend"}
        </Text>
      </Pressable>
    </Card>
  );
}
