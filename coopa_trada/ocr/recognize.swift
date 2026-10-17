import Foundation
import Vision
import ImageIO

let url = URL(fileURLWithPath: CommandLine.arguments[1])
let request = VNRecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false
request.recognitionLanguages = ["en-US"]
let handler = VNImageRequestHandler(url: url)
do {
    try handler.perform([request])
    let cells: [[String: Any]] = (request.results ?? []).compactMap { observation in
        guard let text = observation.topCandidates(1).first else { return nil }
        let box = observation.boundingBox
        return ["text": text.string, "x": box.midX, "y": 1 - box.midY,
                "height": box.height, "confidence": text.confidence]
    }
    let data = try JSONSerialization.data(withJSONObject: cells)
    print(String(data: data, encoding: .utf8)!)
} catch {
    fputs("Could not read image: \(error)\n", stderr)
    exit(1)
}
