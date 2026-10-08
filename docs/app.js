document.addEventListener('DOMContentLoaded', () => {
    const contentDiv = document.getElementById('content');
    const modal = document.getElementById('modal');
    const modalTitle = document.getElementById('modal-title');
    const modalEpisodes = document.getElementById('modal-episodes');
    const closeBtn = document.querySelector('.close');

    fetch('series.json')
        .then(response => response.json())
        .then(data => {
            contentDiv.innerHTML = '';
            
            // تجميع الأعمال حسب التصنيف
            const categories = {};
            data.forEach(item => {
                if (!categories[item.category]) categories[item.category] = [];
                categories[item.category].push(item);
            });

            // عرض كل تصنيف
            for (const [category, items] of Object.entries(categories)) {
                const row = document.createElement('div');
                row.className = 'category-row';
                
                const title = document.createElement('div');
                title.className = 'category-title';
                title.textContent = category;
                row.appendChild(title);

                const grid = document.createElement('div');
                grid.className = 'series-grid';

                items.forEach(item => {
                    const card = document.createElement('div');
                    card.className = 'series-card';
                    card.innerHTML = `
                        <img src="${item.poster || 'https://via.placeholder.com/300x450?text=No+Poster'}" alt="${item.name}" loading="lazy">
                        <div class="series-info">
                            <h3>${item.name}</h3>
                        </div>
                    `;
                    card.addEventListener('click', () => openModal(item));
                    grid.appendChild(card);
                });

                row.appendChild(grid);
                contentDiv.appendChild(row);
            }
        })
        .catch(err => {
            contentDiv.innerHTML = '<div class="loading">حدث خطأ في تحميل البيانات. تأكد من وجود ملف series.json</div>';
            console.error(err);
        });

    function openModal(item) {
        modalTitle.textContent = item.name;
        modalEpisodes.innerHTML = '';
        
        item.episodes.forEach(ep => {
            const btn = document.createElement('a');
            btn.className = 'ep-btn';
            btn.textContent = `حلقة ${ep.ep_num}`;
            // ملاحظة: روابط تليجرام المباشرة تحتاج إلى معالج أو بوت، هذا رابط أساسي
            btn.href = `https://t.me/c/${window.location.hostname.includes('github') ? '' : ''}/${ep.msg_id}`; 
            btn.target = "_blank";
            modalEpisodes.appendChild(btn);
        });
        
        modal.style.display = 'block';
    }

    closeBtn.onclick = () => modal.style.display = 'none';
    window.onclick = (event) => {
        if (event.target == modal) modal.style.display = 'none';
    }
});