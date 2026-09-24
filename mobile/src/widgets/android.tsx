// Android: the headless widget task (the OS's half-hourly update, the
// add/resize events) and the app-triggered redraw. Both draw through
// glance-widget from the same decision (glance-pure.glanceFace); the
// task fetches first, the redraw reads the cache the app just filled.
import { requestWidgetUpdate, type WidgetInfo,
         type WidgetTaskHandlerProps } from "react-native-android-widget";

import { fetchGlanceForWidget, readGlanceCache,
         readWidgetCredential } from "../lib/glance";
import { glanceFace } from "../lib/glance-pure";
import { glanceWidget } from "./glance-widget";

/** The widget's name in app.json's react-native-android-widget config. */
export const WIDGET_NAME = "Glance";

async function faceNow(fetchFirst: boolean) {
  const cred = await readWidgetCredential();
  const cache = fetchFirst && cred ? await fetchGlanceForWidget()
                                   : await readGlanceCache();
  // fetchGlanceForWidget drops a refused credential: re-read it so a
  // revoked widget says "Sign in" on this very redraw
  const signedIn = !!(fetchFirst ? await readWidgetCredential() : cred);
  return glanceFace(cache, signedIn, Date.now());
}

export async function widgetTaskHandler(props: WidgetTaskHandlerProps) {
  switch (props.widgetAction) {
    case "WIDGET_ADDED":
    case "WIDGET_UPDATE":
    case "WIDGET_RESIZED": {
      const face = await faceNow(true);
      props.renderWidget(glanceWidget(face, props.widgetInfo.width));
      return;
    }
    default:
      // WIDGET_CLICK is OPEN_APP, handled by the platform; DELETED has
      // nothing to clean up — the cache is one small item
      return;
  }
}

export async function redrawAndroidWidgets() {
  await requestWidgetUpdate({
    widgetName: WIDGET_NAME,
    renderWidget: async (info: WidgetInfo) =>
      glanceWidget(await faceNow(false), info.width),
  });
}
