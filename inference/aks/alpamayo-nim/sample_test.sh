base64 -i ./driving-scene-sample1.jpeg | tr -d '\n' | jq -Rs '{
  model: "nvidia/alpamayo1.5",
  messages: [{
    role: "user",
    content: [
      {type: "text", text: "Describe this driving scene and identify safety-relevant observations."},
      {type: "image_url", image_url: {url: ("data:image/jpeg;base64," + .)}}
    ]
  }],
  max_tokens: 256
}' | curl -s http://localhost:8000/v1/vqa \
  -H "Content-Type: application/json" \
  --data-binary @- | jq