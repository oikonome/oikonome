// An empty list says what will appear in it and offers the one step that
// fills it. A list that is empty because of a search says so instead and
// offers nothing: the way out is the search box the person just used.
// The phone's twin (mobile/src/components/loaded.tsx) takes the same props.
import { Link } from "react-router";

export default function EmptyState({ title, text, to, action, onAction }: {
  title: string; text?: string; to?: string; action?: string;
  /** the action as a button that does the step here, instead of `to` */
  onAction?: () => void;
}) {
  return (
    <div className="empty">
      <b>{title}</b>
      {text && <p className="sub">{text}</p>}
      {to && action && <Link className="btn pri" to={to}>{action}</Link>}
      {!to && action && onAction && (
        <button type="button" className="pri" onClick={onAction}>{action}</button>
      )}
    </div>
  );
}
