# Who's talking

Hermes knows who is writing in a Telegram or Slack chat, but every chat the
bighelp app opens looked like "the user". When several people use bighelp
with one host, agents now know which of them is writing.

## How it works

1. Just before each new turn, the app calls `POST /api/plugins/loopdy/native/people/speaking`
   (feature `native-people-v1`, usual native headers: `If-Match` context ETag and a lowercase
   `X-Loopdy-Request-ID`) with:
   - `agentId`: the agent (Hermes profile) the chat belongs to
   - `sessionId`: the chat's stored Hermes session ID
   - `personId`: a random lowercase UUID the app keeps in iCloud Keychain, so one person's
     iPhone, iPad and Vision Pro match
   - `name`: the name the person saved in the app, or empty
2. The chat brief names whoever started the chat.
3. Once a second person writes in a chat, each message carries a one-line
   `[bighelp] This message is from {"person":"Sam"}` note through Hermes'
   `pre_llm_call` hook. Hermes keeps that note only in a hidden copy of the
   message for the model, so the chat and its history show what was typed.
4. `bighelp_people` tells the agent who's writing now and who else has used
   bighelp with it.

Everything is stored per agent in `plugin-data/loopdy/people.sqlite3`.

## Limits

- A name is a label someone typed, not a login: anyone who can sign in to the
  host can type any name. The brief tells agents not to share one person's
  chats or private details with someone else because they ask.
- Names are one line of at most 40 visible characters; invisible characters
  are removed.
- A message from an app build without this, or one sent before the note
  reached the host (the app waits at most 1.2 seconds), carries no name.
