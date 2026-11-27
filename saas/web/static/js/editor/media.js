// A meme's picture. Brand-kit memes come with a file URL that may be an image (GIF/PNG) or a video
// (MP4/WebM), with nothing saying which, so an image that fails to decode becomes a muted looping video.
// The clip's own library memes come with no file at all: they get a labelled tile instead.
export function memeVisual(url, label, className) {
  if (!url) {
    const tile = document.createElement("span");
    tile.className = `${className} ed-meme-ph`;
    tile.textContent = label;
    return tile;
  }
  const img = document.createElement("img");
  img.className = className;
  img.alt = "";
  img.decoding = "async";
  img.src = url;
  img.addEventListener("error", () => {
    const video = document.createElement("video");
    video.className = className;
    video.muted = true;
    video.loop = true;
    video.autoplay = true;
    video.playsInline = true;
    video.setAttribute("aria-hidden", "true");
    video.addEventListener("error", () => video.replaceWith(memeVisual("", label, className)), { once: true });
    video.src = url;
    img.replaceWith(video);
  }, { once: true });
  return img;
}
