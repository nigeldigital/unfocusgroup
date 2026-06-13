// Wire up every up/down vote widget on the page.
// Each widget posts to the server and updates its own score in place,
// so the page never reloads just to register a vote.

document.querySelectorAll("[data-vote-widget]").forEach((widget) => {
  const id = widget.dataset.id;
  const loggedIn = widget.dataset.loggedIn === "yes";
  const scoreEl = widget.querySelector("[data-score]");
  const upButton = widget.querySelector(".vote-up");
  const downButton = widget.querySelector(".vote-down");

  function paint(myVote) {
    upButton.classList.toggle("text-tiffany", myVote === 1);
    downButton.classList.toggle("text-rose-500", myVote === -1);
  }

  function castVote(value) {
    // Not logged in? Send them to log in and come back.
    if (!loggedIn) {
      window.location.href = "/login?next=" + encodeURIComponent(window.location.pathname);
      return;
    }

    const body = new URLSearchParams({ value: String(value) });
    fetch(`/feedback/${id}/vote`, { method: "POST", body })
      .then((response) => response.json())
      .then((data) => {
        if (data.error) return;
        scoreEl.textContent = data.score;
        widget.dataset.myVote = data.my_vote;
        paint(data.my_vote);
      })
      .catch(() => {});
  }

  upButton.addEventListener("click", () => castVote(1));
  downButton.addEventListener("click", () => castVote(-1));
});
