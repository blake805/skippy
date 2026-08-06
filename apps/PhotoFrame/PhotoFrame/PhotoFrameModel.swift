import Foundation
import SwiftUI
import Combine

@MainActor
final class PhotoFrameModel: ObservableObject {
    @Published var currentPhotoIndex: Int = 0
    @Published var isPlaying: Bool = true
    @Published var photos: [Photo] = [
        Photo(imageName: "sample1", caption: "Sarah at the beach", date: "June 2023"),
        Photo(imageName: "sample2", caption: "Sarah's birthday", date: "March 2024"),
        Photo(imageName: "sample3", caption: "Sarah in the mountains", date: "August 2023")
    ]
    
    @Published var settings = PhotoFrameSettings()
    
    private var timer: Timer?
    private var cancellables = Set<AnyCancellable>()
    
    var currentPhoto: Photo {
        guard !photos.isEmpty else { return Photo(imageName: "", caption: "", date: "") }
        return photos[currentPhotoIndex]
    }
    
    init() {
        setupTimer()
        
        // Listen for settings changes
        settings.objectWillChange.sink { [weak self] _ in
            self?.objectWillChange.send()
            self?.restartTimer()
        }.store(in: &cancellables)
    }
    
    func nextPhoto() {
        guard !photos.isEmpty else { return }
        currentPhotoIndex = (currentPhotoIndex + 1) % photos.count
    }
    
    func previousPhoto() {
        guard !photos.isEmpty else { return }
        currentPhotoIndex = (currentPhotoIndex - 1 + photos.count) % photos.count
    }
    
    func togglePlayPause() {
        isPlaying.toggle()
        if isPlaying {
            setupTimer()
        } else {
            invalidateTimer()
        }
    }
    
    private func setupTimer() {
        invalidateTimer()
        guard isPlaying else { return }
        
        timer = Timer.scheduledTimer(withTimeInterval: settings.slideDuration, repeats: true) { [weak self] _ in
            self?.nextPhoto()
        }
    }
    
    private func invalidateTimer() {
        timer?.invalidate()
        timer = nil
    }
    
    private func restartTimer() {
        if isPlaying {
            setupTimer()
        }
    }
    
    deinit {
        invalidateTimer()
    }
}

struct Photo: Identifiable {
    let id = UUID()
    let imageName: String
    let caption: String
    let date: String
}

struct PhotoFrameSettings: ObservableObject {
    @Published var slideDuration: Double = 5.0  // seconds
    @Published var showCaptions: Bool = true
    @Published var showDate: Bool = true
    @Published var enableMusic: Bool = true
    @Published var musicVolume: Double = 0.7
}
