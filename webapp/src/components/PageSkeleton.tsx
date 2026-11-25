// What a page shows while its first payload is on the way: quiet blocks in
// the shape of a page (a heading, a figure, a few lines), so the arrival
// of the data fills a layout in rather than replacing a word and shoving
// everything down. Screen readers hear "loading" once; the blocks are
// decoration.
export default function PageSkeleton() {
  return (
    <div role="status" aria-live="polite" style={{ marginTop: "1.2rem" }}>
      <span className="sr-only">loading</span>
      <div aria-hidden="true">
        <div className="sk" style={{ height: "1.7rem", width: "9rem" }} />
        <div className="card">
          <div className="sk" style={{ height: "2.4rem", width: "14rem" }} />
          <div className="sk" style={{ height: ".7rem", width: "100%",
                                       marginTop: "1.1rem" }} />
          <div className="sk" style={{ height: ".7rem", width: "82%",
                                       marginTop: ".7rem" }} />
          <div className="sk" style={{ height: ".7rem", width: "64%",
                                       marginTop: ".7rem" }} />
        </div>
        <div className="card">
          <div className="sk" style={{ height: "1.1rem", width: "11rem" }} />
          <div className="sk" style={{ height: "6rem", width: "100%",
                                       marginTop: "1rem" }} />
        </div>
      </div>
    </div>
  );
}
