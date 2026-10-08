// Upload form handling
document.addEventListener('DOMContentLoaded', function() {
    const form = document.getElementById('uploadForm');
    const fileInput = document.getElementById('pdf');
    const fileName = document.getElementById('fileName');
    const submitBtn = document.getElementById('submitBtn');
    const resultBox = document.getElementById('result');
    const fileLabel = document.querySelector('.file-label');
    const dropZone = document.querySelector('.file-upload');
    const venueSelect = document.getElementById('venue');
    const customVenueGroup = document.getElementById('customVenueGroup');
    const customVenueInput = document.getElementById('customVenue');

    // Maximum file size: 10MB in bytes
    const MAX_FILE_SIZE = 10 * 1024 * 1024;

    // Handle venue selection change
    venueSelect.addEventListener('change', function() {
        if (venueSelect.value === 'Other') {
            customVenueGroup.style.display = 'block';
            customVenueInput.required = true;
        } else {
            customVenueGroup.style.display = 'none';
            customVenueInput.required = false;
            customVenueInput.value = '';
        }
    });

    // Update file name display
    function updateFileName(file) {
        if (file) {
            fileName.textContent = `✓ ${file.name}`;
            fileName.style.display = 'block';
            // Change the upload area appearance
            fileLabel.classList.add('border-green-500', 'bg-green-50');
            fileLabel.classList.remove('border-gray-300', 'bg-gradient-to-br', 'from-gray-50', 'to-white');
            // Update the text inside the upload area
            const fileTextElement = fileLabel.querySelector('.file-text');
            if (fileTextElement) {
                fileTextElement.textContent = 'File selected! Click to change';
            }
        } else {
            fileName.textContent = '';
            fileName.style.display = 'none';
            // Reset the upload area appearance
            fileLabel.classList.remove('border-green-500', 'bg-green-50');
            fileLabel.classList.add('border-gray-300', 'bg-gradient-to-br', 'from-gray-50', 'to-white');
            // Reset the text inside the upload area
            const fileTextElement = fileLabel.querySelector('.file-text');
            if (fileTextElement) {
                fileTextElement.textContent = 'Choose PDF file or drag and drop';
            }
        }
    }

    // Validate file size
    function validateFileSize(file) {
        if (file && file.size > MAX_FILE_SIZE) {
            alert(`File size exceeds 10MB limit. Your file is ${(file.size / (1024 * 1024)).toFixed(2)}MB.`);
            return false;
        }
        return true;
    }

    // File input change handler
    fileInput.addEventListener('change', function(e) {
        const file = e.target.files[0];
        if (file && !validateFileSize(file)) {
            fileInput.value = '';
            fileName.textContent = '';
            return;
        }
        updateFileName(file);
    });

    // Drag and drop handlers
    ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eventName => {
        dropZone.addEventListener(eventName, preventDefaults, false);
    });

    function preventDefaults(e) {
        e.preventDefault();
        e.stopPropagation();
    }

    ['dragenter', 'dragover'].forEach(eventName => {
        dropZone.addEventListener(eventName, function() {
            fileLabel.classList.add('border-stanford-red', 'from-red-50', 'border-solid');
            fileLabel.classList.remove('border-gray-300', 'from-gray-50');
        }, false);
    });

    ['dragleave', 'drop'].forEach(eventName => {
        dropZone.addEventListener(eventName, function() {
            fileLabel.classList.remove('border-stanford-red', 'from-red-50', 'border-solid');
            fileLabel.classList.add('border-gray-300', 'from-gray-50');
        }, false);
    });

    dropZone.addEventListener('drop', function(e) {
        const dt = e.dataTransfer;
        const files = dt.files;

        if (files.length > 0) {
            const file = files[0];
            // Check if it's a PDF
            if (file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf')) {
                // Validate file size
                if (!validateFileSize(file)) {
                    return;
                }
                // Create a new FileList-like object
                const dataTransfer = new DataTransfer();
                dataTransfer.items.add(file);
                fileInput.files = dataTransfer.files;
                updateFileName(file);
            } else {
                alert('Please upload a PDF file.');
            }
        }
    }, false);

    // Form submission handler
    form.addEventListener('submit', async function(e) {
        e.preventDefault();

        // Validate file size before submission
        const file = fileInput.files[0];
        if (!file) {
            alert('Please select a PDF file to upload.');
            return;
        }
        if (!validateFileSize(file)) {
            return;
        }

        // Disable submit button
        submitBtn.disabled = true;
        submitBtn.textContent = 'Preparing upload...';

        // Hide previous results
        resultBox.style.display = 'none';

        // Use custom venue if "Other" is selected, empty string if none selected
        const venueValue = venueSelect.value === 'Other' ? customVenueInput.value : venueSelect.value;
        const emailValue = document.getElementById('email').value;

        try {
            // Step 1: Get presigned URL from server
            submitBtn.textContent = 'Requesting upload URL...';
            const urlResponse = await fetch('/api/get-upload-url', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json'
                },
                body: JSON.stringify({
                    filename: file.name,
                    venue: venueValue || ''
                })
            });

            // Check for rate limit
            if (urlResponse.status === 429) {
                const data = await urlResponse.json();
                const error = new Error(data.detail || 'Rate limit exceeded. Please try again later.');
                error.isRateLimit = true;
                throw error;
            }

            if (!urlResponse.ok) {
                const data = await urlResponse.json();
                throw new Error(data.detail || 'Failed to get upload URL');
            }

            const urlData = await urlResponse.json();

            if (!urlData.success || !urlData.presigned_url || !urlData.s3_key || !urlData.presigned_fields) {
                throw new Error('Invalid response from server');
            }

            // Step 2: Upload file directly to S3 using presigned POST
            submitBtn.textContent = 'Uploading file...';

            // Create FormData with presigned fields first, then the file
            const s3FormData = new FormData();

            // Add all presigned fields (must come before the file)
            for (const [key, value] of Object.entries(urlData.presigned_fields)) {
                s3FormData.append(key, value);
            }

            // Add the file last (required by S3 presigned POST)
            s3FormData.append('file', file);

            const s3Response = await fetch(urlData.presigned_url, {
                method: 'POST',
                body: s3FormData
                // No Content-Type header - browser sets it with boundary for multipart/form-data
            });

            if (!s3Response.ok) {
                throw new Error(`S3 upload failed: ${s3Response.statusText}`);
            }

            // Step 3: Confirm upload with server
            submitBtn.textContent = 'Finalizing submission...';
            const confirmFormData = new FormData();
            confirmFormData.append('s3_key', urlData.s3_key);
            confirmFormData.append('venue', venueValue || '');
            confirmFormData.append('email', emailValue);

            const confirmResponse = await fetch('/api/confirm-upload', {
                method: 'POST',
                body: confirmFormData
            });

            // Check for rate limit
            if (confirmResponse.status === 429) {
                const data = await confirmResponse.json();
                const error = new Error(data.detail || 'Rate limit exceeded. Please try again later.');
                error.isRateLimit = true;
                throw error;
            }

            if (!confirmResponse.ok) {
                const data = await confirmResponse.json();
                throw new Error(data.detail || 'Failed to confirm upload');
            }

            const data = await confirmResponse.json();

            if (data.success) {
                // Show success message
                resultBox.className = 'bg-green-50 border-l-4 border-green-500 rounded-lg p-6 text-green-800';
                resultBox.innerHTML = `
                    <h4 class="text-lg font-semibold mb-3">✓ Submission Successful!</h4>
                    <p class="mb-3">${data.message}</p>
                    <div class="bg-red-100 border-2 border-red-400 rounded-lg p-4 mb-3">
                        <div class="flex items-start gap-3">
                            <i data-lucide="alert-circle" class="w-5 h-5 text-red-700 flex-shrink-0 mt-0.5"></i>
                            <div class="flex-1">
                                <p class="text-red-900 text-sm font-bold mb-1">⚠️ IMPORTANT: Save Your Token Now!</p>
                                <p class="text-red-800 text-sm">
                                    We're experiencing delivery issues with certain email addresses. Please copy and save your token below - you may not receive an email notification.
                                </p>
                            </div>
                        </div>
                    </div>
                    <div class="bg-blue-100 border-2 border-blue-300 rounded-lg p-4 mb-3">
                        <div class="flex items-start gap-3">
                            <i data-lucide="key" class="w-5 h-5 text-blue-700 flex-shrink-0 mt-0.5"></i>
                            <div class="flex-1">
                                <p class="text-blue-900 text-sm font-semibold mb-1">Your Review Token</p>
                                <p class="text-blue-800 text-sm mb-2">
                                    Save this token to view your review later:
                                </p>
                                <div class="flex items-center gap-2">
                                    <code class="flex-1 bg-white border border-blue-300 rounded px-3 py-2 text-blue-900 font-mono text-sm break-all" id="tokenDisplay">${data.token}</code>
                                    <button onclick="copyToken('${data.token}', event)" class="flex-shrink-0 bg-blue-600 hover:bg-blue-700 text-white px-3 py-2 rounded text-sm font-semibold transition-colors duration-200">
                                        Copy
                                    </button>
                                </div>
                            </div>
                        </div>
                    </div>
                    <div class="bg-amber-100 border-2 border-amber-300 rounded-lg p-4">
                        <div class="flex items-start gap-3">
                            <i data-lucide="clock" class="w-5 h-5 text-amber-700 flex-shrink-0 mt-0.5"></i>
                            <div>
                                <p class="text-amber-900 text-sm font-semibold mb-1">Please be patient</p>
                                <p class="text-amber-800 text-sm">
                                    Processing can take hours or even longer if server load is high. Please do not resubmit your paper if you don't receive an email immediately.
                                </p>
                            </div>
                        </div>
                    </div>
                `;
                resultBox.style.display = 'block';

                // Reinitialize Lucide icons for the new content
                lucide.createIcons();

                // Reset form
                form.reset();
                updateFileName(null);
            } else {
                throw new Error(data.detail || data.message || 'Upload failed');
            }
        } catch (error) {
            // Show error message with special styling for rate limit
            const isRateLimit = error.isRateLimit === true;
            resultBox.className = isRateLimit
                ? 'bg-yellow-50 border-l-4 border-yellow-500 rounded-lg p-6 text-yellow-800'
                : 'bg-red-50 border-l-4 border-red-500 rounded-lg p-6 text-red-800';

            resultBox.innerHTML = `
                <h4 class="text-lg font-semibold mb-2">${isRateLimit ? '⚠️' : '✗'} ${isRateLimit ? 'Rate Limit Exceeded' : 'Upload Failed'}</h4>
                <p>${error.message}</p>
            `;
            resultBox.style.display = 'block';
        } finally {
            // Re-enable submit button
            submitBtn.disabled = false;
            submitBtn.textContent = 'Submit for Review';
        }
    });
});

// Function to copy token to clipboard
function copyToken(token, event) {
    navigator.clipboard.writeText(token).then(() => {
        // Show feedback
        const button = event.target;
        const originalText = button.textContent;
        button.textContent = 'Copied!';
        button.classList.add('bg-green-600');
        button.classList.remove('bg-blue-600', 'hover:bg-blue-700');

        // Reset button after 2 seconds
        setTimeout(() => {
            button.textContent = originalText;
            button.classList.remove('bg-green-600');
            button.classList.add('bg-blue-600', 'hover:bg-blue-700');
        }, 2000);
    }).catch(err => {
        console.error('Failed to copy token:', err);
        alert('Failed to copy token. Please copy it manually.');
    });
}
