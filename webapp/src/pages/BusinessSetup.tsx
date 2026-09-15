// /business/setup — the guided business flow as its OWN ROUTE, the way the
// Welcome wizard works.
//
// Completion marks live in tenant config (biz_wizard_steps), not in React
// state: held in the page, a refresh forgets everything — the flow cannot
// resume where you left off, and the "continue setup" prompt never ends
// because nothing recorded that it was finished.
import { useQuery } from "@tanstack/react-query";
import { Navigate, useNavigate } from "react-router";
import { isViewer } from "../role";
import { api } from "../api/client";
import BusinessWizard from "../components/BusinessWizard";

export default function BusinessSetup() {
  // the wizard's exits WRITE the setup mark — a viewer would fill in the
  // whole questionnaire and 403 on the last step (the embedded wizard
  // already excludes viewers; the standalone route must too)
  const meQ = useQuery({ queryKey: ["me"], queryFn: api.me });
  const nav = useNavigate();
  const sum = useQuery({ queryKey: ["biz-summary"],
                         queryFn: api.businessSummary });

  if (sum.isPending)
    return <div className="centered"><span className="mut">loading…</span></div>;

  // A FAILED fetch is not "no business yet". Falling through on
  // error made the anti-duplication guard below read an empty list and offer
  // to create a second entity — a flow whose whole job is to not do that.
  if (sum.isError)
    return (
      <div className="centered">
        <p>Couldn't load your business setup.</p>
        <p className="sub">Not retrying automatically: if you already have a
          business, continuing from here would create a second one.</p>
        <button className="btn pri" onClick={() => sum.refetch()}>
          Try again</button>
      </div>
    );

  // an existing entity means a run was started before — continue THAT one
  // rather than silently creating a second business
  const entity = (sum.data?.entities || [])[0];

  if (isViewer(meQ)) return <Navigate to="/business" replace />;
  return (
    <>
      <h1>Set up a business</h1>
      <p className="mut" style={{ maxWidth: "36rem" }}>
        A short walkthrough so the business's money is kept separate from
        yours from day one. You can skip any step and change everything
        later.
      </p>
      <BusinessWizard
        resume={entity ? { id: entity.id, name: entity.name,
                           business_start_date: entity.business_start_date }
                       : undefined}
        onDone={() => nav("/business")}
        onCancel={() => nav("/business")} />
    </>
  );
}
