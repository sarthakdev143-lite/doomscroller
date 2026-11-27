// A meme's picture. GET /edit gives each meme a file URL (`thumb_url` for the clip's own, `url` for the brand
// kit's) and its `media_type`: a video type becomes a muted looping video, anything else an image. An image
// that fails to decode is tried as a video once (a server that didn't say), and a meme with no file at all
// gets a labelled tile.
function videoOf(url, label, className) {
  const video = document.createElement("video");
  video.className = className;
  video.muted = true;
  video.loop = true;
  video.autoplay = true;
  video.playsInline = true;
  video.setAttribute("aria-hidden", "true");
  video.addEventListener("error", () => video.replaceWith(memeVisual("", label, className)), { once: true });
  video.src = url;
  return video;
}

export function memeVisual(url, label, className, mediaType = "") {
  if (!url) {
    const tile = document.createElement("span");
    tile.className = `${className} ed-meme-ph`;
    tile.textContent = label;
    return tile;
  }
  if (String(mediaType).startsWith("video/")) return videoOf(url, label, className);
  const img = document.createElement("img");
  img.className = className;
  img.alt = "";
  img.decoding = "async";
  img.src = url;
  img.addEventListener("error", () => img.replaceWith(mediaType ? memeVisual("", label, className) : videoOf(url, label, className)), { once: true });
  return img;
}
