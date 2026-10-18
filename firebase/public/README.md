Firebase Hosting needs a public directory. Every request is rewritten to the Cloud Run service
(see firebase.json), which serves the landing page, the app and the API, so this folder stays empty.
