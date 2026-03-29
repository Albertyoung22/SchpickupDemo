document.addEventListener('DOMContentLoaded', () => {
    // 1. Scroll Reveal Animation
    const reveals = document.querySelectorAll('[data-reveal]');
    const observer = new IntersectionObserver((entries) => {
        entries.forEach(entry => {
            if (entry.isIntersecting) {
                entry.target.classList.add('revealed');
                observer.unobserve(entry.target);
            }
        });
    }, { threshold: 0.1, rootMargin: '0px 0px -50px 0px' });
    reveals.forEach(el => observer.observe(el));

    // 2. Counter Animation
    const counters = document.querySelectorAll('.counter');
    counters.forEach(counter => {
        const target = counter.parentElement.innerText.includes('s') ? 0.3 : 100;
        let count = 0;
        const updateCount = () => {
            const increment = target / 100;
            if (count < target) {
                count += increment;
                counter.innerText = count.toFixed(1);
                setTimeout(updateCount, 15);
            } else { counter.innerText = target; }
        };
        const heroObserver = new IntersectionObserver((entries) => {
            if(entries[0].isIntersecting) { updateCount(); heroObserver.disconnect(); }
        });
        heroObserver.observe(counter);
    });

    // 3. Real Voice Preview using API
    const playButtons = document.querySelectorAll('.play-btn, .play-btn-light');
    const audioPlayer = new Audio();

    playButtons.forEach(btn => {
        btn.addEventListener('click', async () => {
            const voice = btn.getAttribute('data-voice');
            const text = btn.getAttribute('data-text');
            if (!voice || !text) return;

            const originalText = btn.innerHTML;
            btn.innerHTML = '<span>⏳</span> 載入聲音...';
            btn.disabled = true;

            try {
                const response = await fetch('/api/tts_preview', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ text, voice, rate: "+0%" })
                });

                if (!response.ok) throw new Error('Network response was not ok');

                const blob = await response.blob();
                const url = URL.createObjectURL(blob);
                
                audioPlayer.src = url;
                btn.innerHTML = '<span>🔊</span> 播報中...';
                
                // Active waveform if present
                const waveform = document.querySelector('.waveform');
                if (waveform) waveform.classList.add('active');

                audioPlayer.play();
                
                audioPlayer.onended = () => {
                    btn.innerHTML = originalText;
                    btn.disabled = false;
                    if (waveform) waveform.classList.remove('active');
                    URL.revokeObjectURL(url);
                };

            } catch (error) {
                console.error('TTS Error:', error);
                btn.innerHTML = '<span>❌</span> 錯誤';
                setTimeout(() => {
                    btn.innerHTML = originalText;
                    btn.disabled = false;
                }, 2000);
            }
        });
    });

    // 4. Smooth Scroll
    document.querySelectorAll('a[href^="#"]').forEach(anchor => {
        anchor.addEventListener('click', function (e) {
            e.preventDefault();
            const targetId = this.getAttribute('href');
            if (targetId === "#") return;
            const targetElement = document.querySelector(targetId);
            if (targetElement) {
                window.scrollTo({ top: targetElement.offsetTop - 80, behavior: 'smooth' });
            }
        });
    });

    // 5. Parallax
    window.addEventListener('scroll', () => {
        const visual = document.querySelector('.hero-visual');
        const scrolled = window.scrollY;
        if (visual && scrolled < 800) {
            visual.style.transform = `translateY(${scrolled * 0.05}px)`;
        }
    });

    // 6. Modal Functions
    const modalOverlay = document.getElementById('modal-overlay');
    const modals = document.querySelectorAll('.modal');
    const closeButtons = document.querySelectorAll('.close-modal');

    window.openModal = (modalId) => {
        const targetModal = document.getElementById(modalId);
        if (!targetModal) return;
        modalOverlay.classList.add('active');
        modals.forEach(m => m.style.display = 'none');
        targetModal.style.display = 'block';
        document.body.style.overflow = 'hidden';
    };

    const closeModal = () => {
        modalOverlay.classList.remove('active');
        document.body.style.overflow = '';
    };

    closeButtons.forEach(btn => btn.addEventListener('click', (e) => {
        e.stopPropagation(); closeModal();
    }));

    modalOverlay.addEventListener('click', (e) => {
        if (e.target === modalOverlay) closeModal();
    });

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeModal();
    });
});
